package upstream

// 企业版（enterpriseId 非空）账号的额度查询。
//
// 背景：企业账号的额度**不在个人「资源包」体系内**，个人接口
// `get-user-resource` 对它们恒返回空 Accounts，导致额度被算成 0。
// 企业额度要走专用端点 `POST /v2/billing/meter/get-enterprise-user-usage`。
//
// 口径差异（与个人接口语义相反，务必注意）：
//   - 个人：CapacityRemain 直接就是「剩余」
//   - 企业：`credit` 是**本周期已用**，`limitNum` 是**分配给该账号的额度**，
//     故 remain = limitNum - credit、total = limitNum
//   - `limitNum == -1` 表示不限量（此时 total 返回 -1，由上层按「不限」展示）
//
// 周期字段：cycleStartTime / cycleEndTime / cycleResetTime 均为 UTC+8 墙钟字符串，
// 与个人接口 CycleEndTime 同口径（packageEndLayout）。

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"time"

	"workbuddy2api/internal/auth"
)

// billingMeterPathEnterpriseUsage 企业额度查询端点（CN）。
const billingMeterPathEnterpriseUsage = "/v2/billing/meter/get-enterprise-user-usage"

// enterpriseUsageData get-enterprise-user-usage 的 **data 本体**结构
// （信封由 doJSON 解开，这里不含 code/msg）。
type enterpriseUsageData struct {
	// Credit 本周期已用（与个人口径的 remain 相反）。
	Credit float64 `json:"credit"`
	// LimitNum 分配给该账号的额度；-1 = 不限量。
	LimitNum       float64 `json:"limitNum"`
	CycleStartTime string  `json:"cycleStartTime"`
	CycleEndTime   string  `json:"cycleEndTime"`
	CycleResetTime string  `json:"cycleResetTime"`
}

// IsEnterprise 账号是否为企业版（auth 文件 enterpriseId 非空）。
func IsEnterprise(a *auth.Auth) bool {
	return a != nil && a.EnterpriseID != ""
}

// enterpriseUsage 查询企业账号额度。
//
// 返回：
//   - remain 剩余额度（limitNum - credit，负值钳 0）
//   - total  周期配额（limitNum；不限量时返回 -1）
//   - cycleEnd 周期结束时间（用于 expiring 分桶；解析失败时为零值）
func (c *Client) enterpriseUsage(a *auth.Auth) (remain int64, total int64, cycleEnd time.Time, err error) {
	// 企业端点固定走 billingJSON（不经 billingMeterPaths：那条是个人接口的
	// realm 路由，企业端点没有 global 变体）。
	data, err := c.billingJSON(a, http.MethodPost, billingMeterPathEnterpriseUsage, map[string]any{})
	if err != nil {
		return 0, 0, time.Time{}, err
	}

	// 注意：billingJSON → doJSON **已经解过信封**（其返回的就是 env.Data，
	// 业务码非 0 时 doJSON 早已按错误返回）。因此这里解析的是 data 本体，
	// 不能再套 {code,msg,data} 结构 —— 否则 LimitNum/Credit 全落零值。
	var resp enterpriseUsageData
	if err := json.Unmarshal(data, &resp); err != nil {
		return 0, 0, time.Time{}, fmt.Errorf("enterprise usage parse: %w", err)
	}

	// limitNum == -1 表示不限量：total 记 -1，由上层按「不限」处理。
	if resp.LimitNum < 0 {
		return 0, -1, parseEnterpriseCycleEnd(resp.CycleEndTime), nil
	}

	// 上游 credit 可能是小数（如 325.35），四舍五入后再减，避免累计误差。
	used := int64(math.Round(resp.Credit))
	limit := int64(math.Round(resp.LimitNum))
	remain = limit - used
	if remain < 0 {
		remain = 0
	}
	return remain, limit, parseEnterpriseCycleEnd(resp.CycleEndTime), nil
}

// parseEnterpriseCycleEnd 解析企业周期结束时间（UTC+8 墙钟）。
// 解析失败返回零值 —— 调用方据此跳过 expiring 分桶，不把额度算错。
func parseEnterpriseCycleEnd(s string) time.Time {
	if s == "" {
		return time.Time{}
	}
	if t, err := time.ParseInLocation(packageEndLayout, s, softRateResetLoc); err == nil {
		return t
	}
	return time.Time{}
}
