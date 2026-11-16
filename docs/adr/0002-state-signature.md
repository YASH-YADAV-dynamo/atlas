# ADR-0002: State signature and ids

Status: accepted (Release 0). Hardening may refine rules 3 and 4 with property tests.

## Decision
`signature(snapshot)` is the SHA-256 hex digest of the lines `v1`, the URL template, then the sorted,
de-duplicated element tokens.

1. **URL template.** Scheme and host are dropped (the site is a separate key). A path segment containing
   a digit becomes `{}`. A trailing slash is dropped. The query keeps only its keys: sorted, values
   removed, keys with empty values removed, pagination keys (`page`, `p`, `offset`, `limit`, `cursor`,
   `per_page`) removed. Written as `/orders?q`. So a filtered list is its own state and "search" is a
   real step; the page number is not.
2. **Elements outside lists** give the token `role:name`, with the name lower-cased, whitespace
   collapsed and every run of digits replaced by `{}`.
3. **Elements inside lists** (`Element.in_list`, set by the driver for elements under an ARIA `row`, or
   a `listitem` outside `navigation`) give the token `role:*`. Row count and row text never matter; an
   empty list (no token) is a different state from a non-empty one, on purpose.
4. **Pagination controls** (normalised name in `next`, `previous`, `next page`, `previous page`,
   `first page`, `last page`) are ignored, so list pages do not multiply into variants.
5. Title and page text are never part of the signature.

Ids are `stable_id(...)`: SHA-256 of the parts, reduced to `[1, 2**53 - 1]`. An earlier design used 63 bits;
53 bits are exact in JSON and JavaScript, and collisions are negligible at our scale (millions of nodes
would be needed). Every id includes the namespace and a kind tag (`state`, `act`, `goal`, `event`), so
tenants and node kinds can never collide by construction.

## Consequences
- Same page with different data is the same state; adding or removing a control makes a new state
  (order detail before and after refund are two states, as TestMart intends).
- The pagination vocabulary is a small English list: a known gap, to be revisited.
- Changing any rule changes every signature: bump the `v1` line and re-learn.

## Revision 2 (signature hardening)

Three additions, found by property tests on realistic page variation. `v1` is kept: only pages that
contain such controls or URL segments get a different signature, so existing maps stay valid.

1. **Formatted numbers are one value.** In a control name, a run of digits together with the separators
   inside it (`,` `.` `:` `/` `-`) becomes a single `{}`: "Total $1,299.00", "Updated 12:45" and
   "Due 2026-11-15" normalise like "Total ${}", "Updated {}" and "Due {}".
2. **Numbered pagination is pagination.** A control whose normalised name is `{}`, `page {}` or
   `go to page {}` is ignored, like previous/next.
3. **Opaque ids without digits are ids.** A path segment that is a UUID, or 16 or more hexadecimal
   characters, becomes `{}` even if it happens to contain no digit. Ordinary words never match.

Unchanged: fragments, trailing slashes and empty queries are already ignored; letter case and spacing of
names do not matter; a control that appears or disappears is still a different state.
