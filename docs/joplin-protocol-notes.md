# Joplin Server protocol notes (Phase 6 baseline)

Status: working notes for the **Joplin 3.7.x** Server sync target. They are the
contract `adapters/joplin` implements and the HTTP tests pin. They are a summary
of public client/server behaviour, not a copy of the upstream source.

Sources read for this baseline (Joplin repository, `dev` branch):

| What | File |
| --- | --- |
| Client sync-target driver | `packages/lib/file-api-driver-joplinServer.ts` |
| Client HTTP layer (auth header, error parsing, retry) | `packages/lib/JoplinServerApi.ts` |
| Item text format (serialize/unserialize) | `packages/lib/models/BaseItem.ts` |
| Delta semantics and compression rules | `readme/dev/spec/server_delta_sync.md` |
| Locks (client) | `packages/lib/services/synchronizer/LockHandler.ts` |
| Locks (server model) | `packages/server/src/models/LockModel.ts` |
| Items routes / change model | `packages/server/src/routes/api/items.ts`, `ChangeModel/ChangeModel.ts` |

The client validates ids as `^[a-fA-F0-9]{32}$` and requires `type_` on every
item. A server that gets either wrong fails the client's `unserialize` before
any sync logic runs.

## 1. Transport and addressing

* Base URL is the server root; every path below is relative to it and starts
  with `api/`.
* An item path is `root:/<name>:`. A note or folder is `<id>.md`; a resource
  blob is `.resource/<id>`.
* Requests carry the session id in `X-API-AUTH`. The client also sends
  `X-API-MIN-VERSION: 2.6.0`. The session cookie is accepted by the server but
  the sync client does not use it.
* The client retries once on `403`, after logging in again.

## 2. Sessions

```
POST api/sessions
{"email": "...", "password": "...", "apiKey": "...", "platform": "...", "type": 1, "version": "3.7.x"}
-> 200 {"id": "<session id>", "user_id": "<user id>"}
```

`id` is what the client stores and sends as `X-API-AUTH`. The response is
otherwise opaque to the sync code; `user_id` is read for diagnostics (e.g. the
content base URL).

Logout is `DELETE api/sessions/<session id>`.

## 3. Items

| Operation | Request | Response |
| --- | --- | --- |
| stat | `GET api/items/root:/<name>:` | item metadata, `404` when absent |
| read | `GET api/items/root:/<name>:/content` | raw item text (or bytes) |
| write | `PUT api/items/root:/<name>:/content` | item metadata |
| delete | `DELETE api/items/root:/<name>:` | empty |
| delta | `GET api/items/root:/<path>:/delta?cursor=&limit=` | `{items, cursor, has_more}` |
| children | `GET api/items/root:/<path>/children` | `{items, has_more}` |
| batch write | `PUT api/batch_items` `{items: [{name, body}]}` | `{items: {<name>: {item} \| {error}}}` |
| batch delete | `DELETE api/batch_items` `{items: [name, ...]}` | `{items: {<name>: {}}}` |

Metadata fields the client reads:

```
id, name, updated_time, jop_updated_time, jopItem?
```

`updated_time` is the server's write time, `jop_updated_time` is the item's own
`updated_time` from its text. The presence of `jopItem` is how the client
decides "delta with items" is supported; when it is absent the client fetches
each changed item's content, which is what this bridge does.

## 4. Item text format

`BaseItem.serialize` emits, and `unserialize` accepts:

```
<title>

<body>

key: value
key: value
```

* A note has both title and body; a folder or resource has only the title.
* Parsing scans from the **bottom**: the first blank line separates props from
  the header; the header's first line is the title and, for a note, everything
  after the second line is the body.
* `type_` is mandatory. Values used by sync: `1` note, `2` folder, `3` resource.
* `id` is 32 hex characters (case-insensitive on the wire, canonical lowercase).
* `parent_id` is empty for the root, otherwise the parent's 32-hex id.
* Times are `YYYY-MM-DDTHH:mm:ss.SSSZ` (UTC).
* Resource metadata carries `mime`, `filename`, `size` and `file_extension`; the
  bytes live at `.resource/<id>`.

## 5. Delta

```
GET api/items/root:/<path>:/delta?cursor=<cursor>
-> 200 {
     "items": [{"item_name": "<id>.md", "type": 1,
                "jop_updated_time": 1761811200000}],
     "cursor": "<new cursor>",
     "has_more": false
   }
```

* `type` is the **change** type: `1` create, `2` update, `3` delete.
* The client drops entries whose `item_name` starts with `locks/`, `temp/` or
  `.resource/`; it fetches note/folder content itself.
* `limit` is advisory: compression can return fewer entries. The server
  guarantees "no more than limit".
* When the cursor is invalid the server answers `400` with
  `{"error": "...", "code": "resyncRequired"}`; the client clears its cursor
  and restarts the delta from the beginning.

Compression rules (server-side):

| observed sequence | emitted |
| --- | --- |
| update, update | update |
| create, update | create |
| create, delete | nothing |
| update, delete | delete |

## 6. Locks

```
POST   api/locks        {"type": 1|2, "clientType": 1|2|3, "clientId": "..."}
GET    api/locks        -> {"items": [{"id": "2_1_abc", "type": 2,
                                       "clientType": 1, "clientId": "abc",
                                       "updatedTime": 1761811200000}], "has_more": false}
DELETE api/locks/<type>_<clientType>_<clientId>
```

* `type`: `1` sync (shared), `2` exclusive (single holder).
* `clientType`: `1` desktop, `2` mobile, `3` cli.
* The release id is exactly `<type>_<clientType>_<clientId>`; the client builds
  it itself, so the server must accept that spelling.
* Field names are **camelCase** (`clientType`, `clientId`, `updatedTime`) and the
  time is epoch milliseconds. A lock older than the TTL (three hours upstream)
  is inactive.
* Conflicts: `409` with `code` `hasExclusiveLock` or `hasSyncLock`. The client
  does not wait on these; it aborts the sync and retries later, so the server
  must keep the values honest.

## 7. Errors

Joplin does not use RFC 7807 or the native Revnote envelope. A failed request is:

```json
{"error": "<human readable>", "code": "<string or number>"}
```

The client puts `code` on the thrown error:

* `404` → `stat` returns "absent";
* `409`, `413`, `422` → `rejectedByTarget` (do not retry this item);
* `400` + `resyncRequired` → reset the delta cursor;
* `403` → log in again once;
* `5xx` → retry with backoff.

## 8. What this bridge deliberately does not implement

* `api/files/*` (the older file endpoints); the 3.7 client uses `api/items/*`.
* `api/batch_items` is implemented, but only for `.md` items (resources use the
  single-item path).
* E2EE: items are treated as opaque text only where they are opaque; the bridge
  serializes/deserializes plaintext note items and does not decrypt.
* Sharing (`share_id`), revisions, and the Joplin **Data API**.
