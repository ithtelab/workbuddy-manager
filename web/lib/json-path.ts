/** diff「定位」用的 JSON 路径类型与锚点生成。 */
export type JsonPath = (string | number)[];

/** DOM 锚点：路径进 id 时把非字母数字换掉（`messages.3` → `json-messages.3`）。 */
export function jsonAnchor(path: JsonPath): string {
  return `json-${path.map((seg) => String(seg).replace(/[^\w-]/g, '_')).join('.') || 'root'}`;
}
