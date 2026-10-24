'use client';

import {useState, type ReactNode} from 'react';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {Input} from '@/components/ui/input';
import {errText, settingsApi} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';

/**
 * 重置某个用户的密码（P1-5）。
 *
 * 原先这一步用 `window.prompt`：一个系统弹窗，没法校验、不参与多语言、键盘
 * 无障碍能力也取决于浏览器，而且它与紧挨着的「删除用户」（走 ConfirmDialog）
 * 风格割裂——同一行里两个按钮，一个像产品的一部分，一个像浏览器的一部分。
 *
 * 长度判据（至少 12 位）与后端 `server/security.py` 的 `password_problem`
 * 对齐。前端拦一道只是为了省掉一次必然失败的往返，**它不是安全边界**——
 * 真正的判据（长度、常见弱口令、用户名+数字）仍在后端，所以这里锁的是
 * 确认键，而不是把输入框本身禁掉。
 */
const MIN_PASSWORD = 12;

export function ResetPasswordDialog({
  username,
  trigger,
}: {
  username: string;
  trigger: ReactNode;
}) {
  const t = useT();
  const [pwd, setPwd] = useState('');

  return (
    <ConfirmDialog
      title={t('settings.resetPasswordTitle', {name: username})}
      description={t('settings.resetPasswordDesc')}
      content={
        <Input
          type="password"
          autoComplete="new-password"
          autoFocus
          value={pwd}
          onChange={(e) => setPwd(e.target.value)}
          placeholder={t('settings.newPasswordPlaceholder')}
          aria-label={t('settings.newPasswordPlaceholder')}
        />
      }
      confirmText={t('settings.resetPassword')}
      confirmDisabled={pwd.length < MIN_PASSWORD}
      // 关掉就清空：弹窗实例会随列表行复用，留着上一次的输入等于把刚设的
      // 密码摆在下一个用户眼前。
      onOpenChange={(open) => {
        if (!open) setPwd('');
      }}
      onConfirm={async () => {
        try {
          const r = await settingsApi.updateUser(username, {password: pwd});
          // 改密码会吊销该用户的既有会话。若改的是自己，当前登录态也随之失效
          // ——必须明确告知要去重新登录，否则用户会以为「界面卡住了」
          // （下一次请求就是 401，会被拦截器直接踢去登录页）。
          if (r?.relogin_required) {
            notify.ok(t('settings.passwordUpdated'), t('settings.passwordRelogin'));
            window.setTimeout(() => {
              window.location.href = `${BASE_PATH}/login`;
            }, 1800);
            return;
          }
          notify.ok(t('settings.passwordUpdated'), t('settings.passwordOthersRevoked'));
        } catch (e) {
          notify.err(errText(e));
        }
      }}
      trigger={trigger}
    />
  );
}
