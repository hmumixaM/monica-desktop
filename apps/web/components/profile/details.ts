import { fieldLabel, fieldSource, textValue, visibleField } from "./fields";
import type { Field } from "./types";

/** One stored copy of a detail: a whole field, or one entry inside a bundled object field. */
export type DetailRef = { field_key: string; item?: string };

/** One row of 笔记与资料: every stored copy with the same name, shown and edited as one. */
export type DetailRow = { id: string; key: string; label: string; values: string[]; refs: DetailRef[]; preset?: Preset };

type Preset = { key: string; label: string; aliases: string[]; multiline?: boolean; placeholder?: string };

const BUNDLE = "monica.custom_fields";
const EXCLUDED = new Set(["display_name", "summary", "monica.name", "monica.birthdate_approximate"]);

export const PRESETS: Preset[] = [
  { key: "biography", label: "个人笔记", aliases: ["笔记", "notes", "note", "biography"], multiline: true },
  { key: "monica.nickname", label: "联系人备注", aliases: ["备注", "nickname"] },
  { key: "monica.phone", label: "电话", aliases: ["phone", "手机", "手机号", "电话号码", "mobile", "tel"], placeholder: "如 +1 626 000 0000" },
  { key: "monica.email", label: "邮箱", aliases: ["email", "e-mail", "mail", "邮件", "电子邮箱"] },
  { key: "address", label: "联系地址", aliases: ["地址", "address", "住址"] },
  { key: "monica.company", label: "公司", aliases: ["company", "单位"] },
  { key: "monica.job", label: "职位", aliases: ["job", "title", "职务", "position"] },
  { key: "monica.school", label: "学校", aliases: ["school", "university", "大学"] },
  { key: "monica.residence", label: "常驻地", aliases: ["residence", "居住地", "所在地", "现居"] },
  { key: "monica.hometown", label: "家乡", aliases: ["hometown", "老家"] },
  { key: "monica.interests", label: "兴趣", aliases: ["interests", "爱好", "兴趣爱好", "hobbies"] },
  { key: "monica.food_preferences", label: "饮食偏好", aliases: ["food preferences", "饮食", "忌口"] },
  { key: "monica.family", label: "家人", aliases: ["family", "家庭"] },
  { key: "monica.first_met_where", label: "初次见面地点", aliases: ["first met where", "认识地点"] },
  { key: "monica.first_met_additional_info", label: "相识记录", aliases: ["怎么认识", "how we met"], multiline: true },
];

const normalize = (label: string) => label.trim().replace(/\s+/g, " ").toLowerCase();
const PRESET_BY_NAME = new Map(PRESETS.flatMap((preset) => [preset.key, preset.label, ...preset.aliases].map((name) => [normalize(name), preset] as const)));

export function presetFor(label: string): Preset | undefined {
  return PRESET_BY_NAME.get(normalize(label));
}

/** Rows with the same id are the same detail: one preset, or one custom name ignoring case and spacing. */
export function detailId(label: string): string {
  return presetFor(label)?.key ?? `custom:${normalize(label)}`;
}

type Copy = { ref: DetailRef; label: string; value: string; manual: boolean; plain: boolean; updated: string };

// A name typed by hand may contain words the import filters look for ("LinkedIn", "raw"); it is still the user's own row.
const handWritten = (field: Field) => field.current_source_type === "manual" && !/^(wechat|linkedin|instagram)[.]/.test(field.field_key);

function copies(fields: Field[]): Copy[] {
  return fields.flatMap((field): Copy[] => {
    if (EXCLUDED.has(field.field_key) || !(handWritten(field) || fieldSource(field) === "manual")) return [];
    const manual = field.current_source_type === "manual";
    if (field.field_key === BUNDLE) {
      if (!field.value || typeof field.value !== "object" || Array.isArray(field.value)) return [];
      return Object.entries(field.value as Record<string, unknown>).flatMap(([item, value]) => {
        const shown = { ...field, field_key: `Monica · ${item}`, value };
        return fieldSource(shown) === "manual" && visibleField(shown) && textValue(value).trim()
          ? [{ ref: { field_key: BUNDLE, item }, label: item.trim(), value: textValue(value).trim(), manual, plain: false, updated: field.updated_at }]
          : [];
      });
    }
    if (!(handWritten(field) || visibleField(field)) || !textValue(field.value).trim()) return [];
    const label = presetFor(field.field_key)?.label ?? fieldLabel(field.field_key).trim();
    return [{ ref: { field_key: field.field_key }, label, value: textValue(field.value).trim(), manual, plain: true, updated: field.updated_at }];
  });
}

/** Presets in catalog order, then custom names alphabetically; within a row the newest hand-written value comes first. */
export function detailRows(fields: Field[]): DetailRow[] {
  const groups = new Map<string, Copy[]>();
  for (const copy of copies(fields)) groups.set(detailId(copy.label), [...(groups.get(detailId(copy.label)) ?? []), copy]);
  const rows = [...groups].map(([id, group]): DetailRow => {
    const ordered = [...group].sort((a, b) => Number(b.manual) - Number(a.manual) || Number(b.plain) - Number(a.plain) || b.updated.localeCompare(a.updated));
    const preset = PRESETS.find((item) => item.key === id);
    return {
      id, preset, label: preset?.label ?? ordered[0].label,
      key: preset?.key ?? ordered.find((copy) => copy.plain)?.ref.field_key ?? ordered[0].label,
      values: [...new Set(ordered.map((copy) => copy.value))], refs: ordered.map((copy) => copy.ref),
    };
  });
  const rank = (row: DetailRow) => row.preset ? PRESETS.indexOf(row.preset) : PRESETS.length;
  return rows.sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label, "zh-CN"));
}

/** Where a save under `label` writes, and every stored copy it replaces: the edited row and any row already using that name. */
export function savePlan(rows: DetailRow[], editing: DetailRow | null, label: string): { key: string; replaces: DetailRef[]; merges: DetailRow | undefined } {
  const id = detailId(label);
  const merges = rows.find((row) => row.id === id && row.id !== editing?.id);
  const key = presetFor(label)?.key ?? merges?.key ?? (editing?.id === id ? editing.key : label.trim());
  return { key, merges, replaces: [...(editing?.refs ?? []), ...(merges?.refs ?? [])] };
}
