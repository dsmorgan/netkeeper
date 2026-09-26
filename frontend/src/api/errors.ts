/**
 * The sentence in a FastAPI error body, whichever of its two shapes it came in.
 *
 * A route's own refusal is `{"detail": "..."}`. A request the schema turned
 * away is `{"detail": [{"loc": [...], "msg": "...", ...}, ...]}`: a list, one
 * entry per problem. Reading only the string shape turned the second into a
 * bare status code, so a person who picked too many rows saw "422" and nothing
 * about why.
 *
 * Pydantic prefixes a message raised from a validator with `Value error, `,
 * which is the library talking, not the backend; it is dropped.
 *
 * Returns null when the body carries no message at all.
 */
export function detailMessage(body: unknown): string | null {
  if (body === null || typeof body !== 'object' || !('detail' in body)) return null
  const { detail } = body
  if (typeof detail === 'string') return detail === '' ? null : detail
  if (!Array.isArray(detail)) return null
  const messages = detail
    .map((item: unknown) =>
      item !== null && typeof item === 'object' && 'msg' in item ? item.msg : undefined,
    )
    .filter((msg): msg is string => typeof msg === 'string' && msg !== '')
    .map((msg) => msg.replace(/^Value error, /, ''))
  return messages.length > 0 ? messages.join('; ') : null
}
