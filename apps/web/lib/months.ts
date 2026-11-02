/** Month keys are "YYYY-MM". */
export const currentMonth = () => new Date().toISOString().slice(0, 7);

export function nextMonth(month: string): string {
  const [year, value] = month.split("-").map(Number);
  return value === 12 ? `${year + 1}-01` : `${year}-${String(value + 1).padStart(2, "0")}`;
}

/** Every month from the earliest row through the later of the last row and now; gaps are filled with `empty(month)`. */
export function continuousMonths<T extends { month: string }>(rows: T[], empty: (month: string) => T): T[] {
  if (!rows.length) return [];
  const byMonth = new Map(rows.map((row) => [row.month, row]));
  const sorted = [...byMonth.keys()].sort();
  const last = [currentMonth(), sorted.at(-1)!].sort().at(-1)!;
  const result: T[] = [];
  for (let month = sorted[0]; month <= last; month = nextMonth(month)) result.push(byMonth.get(month) ?? empty(month));
  return result;
}

export const monthLabel = (month: string) => `${month.slice(0, 4)}年${Number(month.slice(5))}月`;
