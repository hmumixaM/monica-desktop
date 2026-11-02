export type Signal = {label: string; values: string[]; more: number};

export type Candidate = {
  profile_id: string;
  display_name: string;
  providers: string[];
  username?: string;
  score: number;
  confidence: string;
  reasons: string[];
  pair: string;
  pair_label: string;
  ready: boolean;
  status: string;
  expected_profile_id: string | null;
  signals?: Signal[];
  /** How alike the account's avatar face is to this person, from the home Mac; 1 once confirmed. */
  face_score?: number | null;
};

export type QueueRow = {
  account_id: string;
  provider: string;
  provider_label: string;
  display_name: string;
  username: string;
  profile_id: string | null;
  profile_name: string;
  ready: boolean;
  blocked_reason: string | null;
  avatar_media_id: string | null;
  pair: string;
  pair_label: string;
  score: number;
  confidence: string;
  status: string;
  conflict: boolean;
  signals: Signal[];
  candidates: Candidate[];
};

export type Review = {
  pairs: {id: string; label: string}[];
  total: number;
  unresolved: number;
  page: number;
  page_size: number;
  queue: QueueRow[];
};

export type LinkedAccount = {
  account_id: string;
  provider: string;
  display_name: string;
  username: string;
  profile_url: string | null;
  avatar_media_id: string | null;
  link_label: string;
  link_reasons: string[] | null;
};

export type LinkedPerson = {
  profile_id: string;
  display_name: string;
  avatar_media_id: string | null;
  providers: string[];
  accounts: LinkedAccount[];
  signals: Signal[];
};

export type StagedAction = "confirm" | "reject" | "skip";

export type StagedItem = {
  account_id: string;
  action: StagedAction;
  profile_id: string;
  expected_profile_id: string | null;
  score: number;
  reasons: string[];
  account_name: string;
  account_provider: string;
  profile_name: string;
  error?: string;
};

export const PROVIDERS = ["wechat", "instagram", "linkedin"] as const;

export const BLOCKER_LABEL: Record<string, string> = {
  quarantined: "旧身份已隔离，不能关联",
  unowned_media: "当前人物有未标来源的头像或封面",
  unowned_tags: "当前人物有未标来源的平台标签",
  unowned_relationships: "当前人物有未标来源的平台关系",
};

const ERROR_LABEL: Record<string, string> = {
  "Target profile already has this provider": "对方已经有同一平台账号",
  "The account link changed; refresh before confirming": "账号归属已变化，请刷新后重选",
  "Source account not found": "来源账号不存在",
  "Profile not found": "目标人物不存在或已归档",
  "This legacy identity is quarantined and cannot be linked": BLOCKER_LABEL.quarantined,
  "The current profile has an avatar or cover without a source account": BLOCKER_LABEL.unowned_media,
  "The current profile has platform tags without a source account": BLOCKER_LABEL.unowned_tags,
  "The current profile has platform relationships without a source account": BLOCKER_LABEL.unowned_relationships,
};

export function errorLabel(detail: unknown) {
  return typeof detail === "string" ? ERROR_LABEL[detail] ?? detail : "保存失败";
}
