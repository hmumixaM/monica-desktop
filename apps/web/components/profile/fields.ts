import type { Field, Identity, Profile, Relationship, StructuredFact } from "./types";

export function fieldSource(field: Field): string {
  if (/^wechat[.]|^Monica · 微信|^Monica · 朋友圈|^Monica · 群/.test(field.field_key)) return "wechat";
  if (/^linkedin[.]|LinkedIn/i.test(field.field_key)) return "linkedin";
  if (/^instagram[.]|Instagram/i.test(field.field_key) || field.current_source_type === "instagram") return "instagram";
  return "manual";
}

const HIDDEN_FIELD = /snapshot|hash|partial|coverage|archived_at|synced_at|match_note|profile_pic|avatar|cover_url|internal_id|group_id|group_stats|direct_stats|moments_summary|location_history|custom_fields|好友来源|统计截至|内部.?ID|群.?ID|群主微信 ID|已关联成员|联系人类型|群活动|群发言统计|群内显示名|私聊统计|共同群|消息数|消息总数|直接消息|群内消息|首次直接|最近直接|首次群内|最近群内|朋友圈关系分|朋友圈最常互动|朋友圈足迹|raw/i;

/** Import bookkeeping and statistics stay out of the readable profile; they live in history and the stats blocks. */
export function visibleField(field: Pick<Field, "field_key" | "value">): boolean {
  if (HIDDEN_FIELD.test(field.field_key)) return false;
  if (typeof field.value === "object" && field.value !== null && !Array.isArray(field.value)) return false;
  return field.value != null && field.value !== "" && (!Array.isArray(field.value) || field.value.length > 0);
}

export function textValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(textValue).join("\n");
  if (typeof value === "string") return value.replace(/\\n/g, "\n");
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  return "";
}

export function summarizeValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value.length > 220 ? `${value.slice(0, 220)}…` : value;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.length ? `${value.length} 项 · ${value.slice(0, 3).map(summarizeValue).join("、")}${value.length > 3 ? "…" : ""}` : "空列表";
  if (typeof value === "object") {
    const item = value as Record<string, unknown>;
    if ("n" in item || "count" in item) return `总计 ${summarizeValue(item.n ?? item.count)}${item.sent !== undefined ? ` · 发 ${summarizeValue(item.sent)}` : ""}${item.recv !== undefined ? ` · 收 ${summarizeValue(item.recv)}` : ""}`;
    if ("places" in item && Array.isArray(item.places)) return `${item.places.length} 个地点`;
    const keys = Object.keys(item);
    return keys.length ? keys.slice(0, 4).map((key) => `${key}: ${summarizeValue(item[key]).slice(0, 54)}`).join(" · ") : "空对象";
  }
  return String(value);
}

export function formatDate(value?: string | null): string {
  if (!value) return "未记录时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

const FIELD_LABELS: Record<string, string> = {
  display_name: "姓名或群名", summary: "简介", biography: "个人笔记", address: "联系地址",
  "monica.nickname": "联系人备注", "monica.food_preferences": "饮食偏好", "monica.first_met_where": "初次见面地点",
  "monica.first_met_additional_info": "相识记录", "monica.job": "职位", "monica.company": "公司",
  "monica.residence": "常驻地", "monica.hometown": "家乡", "monica.school": "学校", "monica.email": "邮箱", "monica.phone": "电话",
  "monica.interests": "兴趣", "monica.family": "家人",
  "wechat.alias": "微信号", "wechat.remark": "微信备注", "wechat.nickname": "微信昵称", "wechat.signature": "微信个性签名",
  "wechat.add_source": "微信好友来源", "wechat.historical_nicknames": "微信历史昵称", "wechat.country": "微信国家", "wechat.province": "微信省份",
  "wechat.city": "微信城市", "wechat.group_id": "微信群 ID", "wechat.owner": "群主微信 ID", "wechat.member_count": "群成员总数",
  "wechat.group_stats": "微信群聊统计", "wechat.direct_stats": "微信私聊统计", "wechat.moments_summary": "朋友圈关系统计", "wechat.location_history": "朋友圈足迹",
  "linkedin.about": "LinkedIn 简介", "linkedin.headline": "LinkedIn 标题", "linkedin.profile_url": "LinkedIn 链接", "linkedin.mutuals": "LinkedIn 共同好友",
  "linkedin.partial": "LinkedIn 覆盖状态", "instagram.biography": "Instagram 简介", "instagram.followers_count": "Instagram 粉丝",
  "instagram.followees_count": "Instagram 关注", "instagram.media_count": "Instagram 帖子数", "instagram.profile_pic_url": "Instagram 头像地址",
};

export function fieldLabel(fieldKey: string): string {
  if (/^instagram.accounts.[^.]+./.test(fieldKey)) return fieldLabel(fieldKey.replace(/^instagram.accounts.[^.]+./, "instagram."));
  return FIELD_LABELS[fieldKey] || fieldKey.replace(/^monica\.|^Monica · /, "").replace(/[._]/g, " · ");
}

export function compactIdentityLabel(identity: Identity): string {
  const value = identity.username || identity.display_name || identity.external_id;
  return value.length > 32 ? `${value.slice(0, 29)}…` : value;
}

/** One chip per account even when several imports observed it. */
export function compactIdentities(identities: Identity[]): (Identity & { sourceCount: number })[] {
  const map = new Map<string, Identity & { sourceCount: number }>();
  for (const identity of identities) {
    const key = `${identity.provider}:${identity.username || identity.profile_url || identity.external_id}`.toLowerCase();
    const current = map.get(key);
    if (current) current.sourceCount += 1;
    else map.set(key, { ...identity, sourceCount: 1 });
  }
  return [...map.values()];
}

export function profileAddress(profile: Profile): string {
  const field = profile.fields?.find((item) => item.field_key.includes("address") || item.field_key.includes("location") || item.field_key === "wechat.city");
  return field ? summarizeValue(field.value) : "";
}

export function distinctRelationships(rows: Relationship[]): Relationship[] {
  const map = new Map<string, Relationship>();
  for (const row of rows) {
    const key = `${row.relationship_type}:${row.related_profile_id}`;
    const old = map.get(key);
    if (!old) map.set(key, { ...row });
    else if (old.direction !== row.direction) old.direction = "both";
  }
  return [...map.values()];
}

export function groupRelationships(rows: Relationship[]): [string, Relationship[]][] {
  const grouped = new Map<string, Relationship[]>();
  for (const row of distinctRelationships(rows)) grouped.set(row.relationship_type, [...(grouped.get(row.relationship_type) ?? []), row]);
  return [...grouped];
}

const RELATIONSHIP_LABELS: Record<string, string> = {
  wechat_contact_card_friend: "微信名片推荐", moments_interaction: "朋友圈互动", friend: "朋友", colleague: "同事", family: "家人", member: "群成员", owner: "群主",
};
export const relationshipLabel = (type: string) => RELATIONSHIP_LABELS[type] || type;

export const roleLabel = (role?: string | null) => role === "owner" ? "群主" : role === "admin" ? "管理员" : "成员";

const factValue = (fact: StructuredFact) => (fact.value && typeof fact.value === "object" ? fact.value : {}) as Record<string, unknown>;

/** Current roles first, then most recent start date (the backend presentation order). */
export function sortFacts(facts: StructuredFact[]): StructuredFact[] {
  const current = (fact: StructuredFact) => factValue(fact).is_current === true || (!factValue(fact).end_date && !!factValue(fact).start_date);
  return [...facts].sort((a, b) => Number(current(b)) - Number(current(a)) || String(factValue(b).start_date || "").localeCompare(String(factValue(a).start_date || "")));
}

export function factParts(fact: StructuredFact) {
  const item = factValue(fact);
  return {
    institution: fact.company_name || fact.school_name || textValue(item.company_name || item.school_name),
    title: textValue(item.title || item.degree || item.position_title),
    field: textValue(item.field_of_study),
    dates: [item.start_date, item.end_date || (item.is_current ? "至今" : null)].filter(Boolean).join(" — "),
    location: textValue(item.location),
    description: textValue(item.description),
    current: item.is_current === true || (!item.end_date && !!item.start_date),
  };
}
