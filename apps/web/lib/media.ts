// Provider URLs are provenance. Only registered NAS assets may be rendered.
const mediaId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function localMediaUrl(id?: string | null): string | undefined {
  return id && mediaId.test(id) ? `/api/v1/media/${id}` : undefined;
}

export function isLocalMediaUrl(url: string): boolean {
  const match = /^\/api\/v1\/media\/([^?]+)$/.exec(url);
  return !!match && mediaId.test(match[1]);
}
