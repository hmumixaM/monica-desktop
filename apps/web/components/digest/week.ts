const day = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric" });

function local(value: string) {
  return new Date(`${value}T00:00:00`);
}

/** "10月5日 – 10月11日" for the week starting Monday `start`. */
export function weekRange(start: string) {
  const first = local(start);
  const last = new Date(first);
  last.setDate(first.getDate() + 6);
  return `${day.format(first)} – ${day.format(last)}`;
}

export function shortDay(value: string) {
  return day.format(local(value.slice(0, 10)));
}
