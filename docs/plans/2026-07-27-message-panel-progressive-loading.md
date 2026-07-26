# Message Panel Progressive Loading Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Render at most 20 messages on group selection, load older messages 20 at a time near the scroll boundary, and make group-switch requests concurrent and cancellable.

**Architecture:** Keep the existing FastAPI/Jinja HTML-partial model. Add one page-aware query helper that returns serialized rows plus `has_more`, use it consistently across the three message routes, and let the existing frontend append returned HTML. Add a single pagination controller for both scroll and button loading, then start strategy/detail requests concurrently under an `AbortController` while retaining the request-ID stale-response guard.

**Tech Stack:** Python 3.13+, FastAPI, SQLAlchemy, Jinja2, vanilla JavaScript, CSS, pytest, FastAPI TestClient, Node.js asset harnesses

---

### Task 1: Add a page-aware message query

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py:281-410`
- Test: `tests/test_web_queries_messages.py`

**Step 1: Write the failing page-boundary tests**

Add tests that create 19, 20, and 21 matching messages and exercise a new `load_group_message_page` helper:

```python
def test_load_group_message_page_returns_twenty_rows_and_has_more(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=message_id, text=f"message {message_id}")
                for message_id in range(1, 22)
            ]
        )
        session.commit()

    messages, has_more = load_group_message_page(
        session_factory,
        chat_id=88,
        page_size=20,
    )

    assert len(messages) == 20
    assert [message["message_id"] for message in messages] == list(range(21, 1, -1))
    assert has_more is True


@pytest.mark.parametrize(("message_count", "expected_has_more"), [(19, False), (20, False)])
def test_load_group_message_page_omits_has_more_on_final_page(
    tmp_path,
    message_count,
    expected_has_more,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=message_id, text=f"message {message_id}")
                for message_id in range(1, message_count + 1)
            ]
        )
        session.commit()

    messages, has_more = load_group_message_page(
        session_factory,
        chat_id=88,
        page_size=20,
    )

    assert len(messages) == message_count
    assert has_more is expected_has_more
```

Add a cursor/filter test proving that `before_message_id`, `search_text`, and `sender_name` are applied before the extra-row calculation.

**Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_web_queries_messages.py -q
```

Expected: FAIL because `load_group_message_page` is not defined.

**Step 3: Extract the shared filtered query builder**

In `web_queries.py`, add an internal helper used by both existing and page-aware loading:

```python
def _group_messages_query(
    session,
    *,
    chat_id: int,
    before_message_id: int | None,
    search_text: str | None,
    sender_name: str | None,
):
    query = session.query(RawMessage).filter(RawMessage.chat_id == chat_id)
    if before_message_id is not None:
        query = query.filter(RawMessage.message_id < before_message_id)
    if search_text:
        search_value = f"%{search_text.strip()}%"
        query = query.filter(
            or_(
                RawMessage.text.ilike(search_value),
                RawMessage.sender_name.ilike(search_value),
            )
        )
    if sender_name:
        sender_value = f"%{sender_name.strip()}%"
        query = query.filter(RawMessage.sender_name.ilike(sender_value))
    return query.order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
```

Update `load_group_messages` to use this helper without changing its return contract.

**Step 4: Implement the page-aware helper**

Add:

```python
def load_group_message_page(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    page_size: int,
    before_message_id: int | None = None,
    search_text: str | None = None,
    sender_name: str | None = None,
) -> tuple[list[dict[str, object | None]], bool]:
    """Load one serialized message page and report whether older rows exist."""

    with session_factory() as session:
        raw_messages = (
            _group_messages_query(
                session,
                chat_id=chat_id,
                before_message_id=before_message_id,
                search_text=search_text,
                sender_name=sender_name,
            )
            .limit(page_size + 1)
            .all()
        )
        has_more = len(raw_messages) > page_size
        return _serialize_raw_messages(session, raw_messages[:page_size]), has_more
```

**Step 5: Run the query tests**

Run:

```bash
uv run pytest tests/test_web_queries_messages.py -q
```

Expected: PASS.

**Step 6: Commit the query change**

```bash
git add src/telegram_kol_research/web_queries.py tests/test_web_queries_messages.py
git commit -m "perf: add bounded message page query"
```

### Task 2: Apply the 20-message contract to every Web message route

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:150-170`
- Modify: `src/telegram_kol_research/web_app.py:4160-4305`
- Modify: `src/telegram_kol_research/web_app.py:5060-5115`
- Modify: `src/telegram_kol_research/templates/_messages.html:1-390`
- Test: `tests/test_web_group_messages_route.py:233-318`
- Test: `tests/test_web_page_render.py:1260-1390`

**Step 1: Write failing route tests for the initial boundary and final footer**

Replace the two-row footer assumption with explicit boundary tests:

```python
def test_group_messages_route_renders_twenty_messages_and_more_footer(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=message_id, text=f"message {message_id}")
                for message_id in range(1, 22)
            ]
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert response.text.count('data-message-card') == 20
    assert "message 21" in response.text
    assert "message 2" in response.text
    assert "message 1" not in response.text
    assert 'data-before-message-id="2"' in response.text
    assert "data-load-more" in response.text


def test_group_messages_route_omits_more_footer_on_final_page(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=message_id, text=f"message {message_id}")
                for message_id in range(1, 21)
            ]
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    )

    assert response.status_code == 200
    assert response.text.count('data-message-card') == 20
    assert "data-load-more" not in response.text
```

Add equivalent assertions for `/groups/88/detail` and `/groups/88/detail/tab/messages` so all entry points share the same page size.

**Step 2: Run the focused route tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_web_group_messages_route.py \
  tests/test_web_page_render.py \
  -q
```

Expected: FAIL because the routes still load 50 messages and the footer does not know `has_more`.

**Step 3: Add the shared Web page-size constant**

Near the Web application constants, add:

```python
MESSAGE_PAGE_SIZE = 20
```

Import `load_group_message_page` next to `load_group_messages`.

**Step 4: Update all three message-rendering routes**

For `group_detail`, `group_detail_tab_messages`, and `group_messages`, replace the fixed 50-row load with:

```python
messages, has_more = load_group_message_page(
    app.state.session_factory,
    chat_id=chat_id,
    page_size=MESSAGE_PAGE_SIZE,
    before_message_id=before_message_id,
    search_text=search_text,
    sender_name=sender_name,
)
```

Only the standalone route supplies cursor and filter values. Add both values to every template context:

```python
"has_more": has_more,
"message_page_size": MESSAGE_PAGE_SIZE,
```

Leave `/api/chat` on `load_group_messages`; its 50/100-message analytical context is not a UI timeline page.

**Step 5: Make the footer depend on `has_more`**

Change the template footer to:

```jinja2
{% set next_before_message_id = messages[-1].message_id if messages else None %}
<div class="message-list-footer" data-message-list-footer>
  {% if has_more and next_before_message_id %}
    <button
      type="button"
      data-load-more
      data-before-message-id="{{ next_before_message_id }}"
    >
      加载更多
    </button>
  {% endif %}
</div>
```

Add `data-message-page-size="{{ message_page_size }}"` to the message-panel root for diagnostics.

**Step 6: Run the focused route and render tests**

Run:

```bash
uv run pytest \
  tests/test_web_group_messages_route.py \
  tests/test_web_page_render.py \
  -q
```

Expected: PASS.

**Step 7: Commit the route contract**

```bash
git add \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_messages.html \
  tests/test_web_group_messages_route.py \
  tests/test_web_page_render.py
git commit -m "perf: limit initial message timeline to twenty"
```

### Task 3: Add automatic history loading with a manual retry fallback

**Files:**
- Modify: `src/telegram_kol_research/static/app.js:1-30`
- Modify: `src/telegram_kol_research/static/app.js:315-425`
- Modify: `src/telegram_kol_research/static/app.js:900-1000`
- Modify: `src/telegram_kol_research/static/app.css:3521`
- Test: `tests/test_web_assets_smoke.py:297-390`

**Step 1: Write failing JavaScript asset assertions**

Add a focused test:

```python
def test_app_js_loads_older_messages_once_near_scroll_boundary(tmp_path):
    js = TestClient(
        create_web_app(database_path=tmp_path / "research.db")
    ).get("/static/app.js").text

    assert "const MESSAGE_LOAD_MORE_THRESHOLD = 320;" in js
    assert "async function loadMoreMessages(panel)" in js
    assert "loadMoreButton.dataset.loading === 'true'" in js
    assert "scrollHeight - scrollTop - clientHeight" in js
    assert "loadMoreMessages(panel);" in js
    assert "加载失败，点击重试" in js
    assert "currentList.insertAdjacentHTML('beforeend', nextList.innerHTML);" in js
```

Keep the existing assertion that older history is appended at the end.

**Step 2: Run the asset tests and verify they fail**

Run:

```bash
uv run pytest tests/test_web_assets_smoke.py -q
```

Expected: FAIL because the automatic pagination controller does not exist.

**Step 3: Add the threshold and shared loading function**

Add:

```javascript
const MESSAGE_LOAD_MORE_THRESHOLD = 320;
```

Extract the existing button callback into:

```javascript
async function loadMoreMessages(panel) {
  const loadMoreButton = panel?.querySelector('[data-load-more]');
  if (!loadMoreButton || loadMoreButton.dataset.loading === 'true') return;

  const { chatId, searchText, senderName } = getMessageFilterState(panel);
  const beforeMessageId = Number(loadMoreButton.dataset.beforeMessageId || '0');
  if (!chatId || !beforeMessageId) return;

  loadMoreButton.dataset.loading = 'true';
  loadMoreButton.disabled = true;
  loadMoreButton.textContent = '加载中…';
  try {
    const nextPanel = await fetchMessagePanel(chatId, {
      beforeMessageId,
      searchText,
      senderName,
    });
    if (!panel.isConnected || Number(panel.dataset.chatId || '0') !== chatId) return;

    const nextList = nextPanel?.querySelector('[data-message-list]');
    const currentList = panel.querySelector('[data-message-list]');
    if (currentList && nextList) {
      currentList.insertAdjacentHTML('beforeend', nextList.innerHTML);
    }

    const currentFooter = panel.querySelector('[data-message-list-footer]');
    const nextFooter = nextPanel?.querySelector('[data-message-list-footer]');
    if (currentFooter && nextFooter) currentFooter.replaceWith(nextFooter);
    bindMessagePanelControls(panel);
  } catch {
    if (!panel.isConnected) return;
    loadMoreButton.dataset.loading = 'false';
    loadMoreButton.disabled = false;
    loadMoreButton.textContent = '加载失败，点击重试';
  }
}
```

**Step 4: Bind the button and scroll threshold to the same function**

In `bindMessagePanelControls`, guard duplicate bindings with dataset flags:

```javascript
const loadMoreButton = panel.querySelector('[data-load-more]');
if (loadMoreButton && loadMoreButton.dataset.loadMoreBound !== 'true') {
  loadMoreButton.dataset.loadMoreBound = 'true';
  loadMoreButton.addEventListener('click', () => loadMoreMessages(panel));
}

const scrollContainer = getMessageScrollContainer(panel);
if (scrollContainer && scrollContainer.dataset.historyScrollBound !== 'true') {
  scrollContainer.dataset.historyScrollBound = 'true';
  scrollContainer.addEventListener('scroll', () => {
    const remaining = (
      scrollContainer.scrollHeight
      - scrollContainer.scrollTop
      - scrollContainer.clientHeight
    );
    if (remaining <= MESSAGE_LOAD_MORE_THRESHOLD) loadMoreMessages(panel);
  }, { passive: true });
}
```

Do not automatically load on initial binding; loading begins only after user scrolling or fallback-button activation.

**Step 5: Add compact footer state styling**

Keep the footer stable while loading and make the fallback a normal secondary control:

```css
.message-list-footer {
  min-height: 48px;
  padding: 10px 14px;
}

.message-list-footer:empty {
  min-height: 0;
  padding: 0;
}
```

**Step 6: Run the asset tests**

Run:

```bash
uv run pytest tests/test_web_assets_smoke.py -q
```

Expected: PASS.

**Step 7: Commit progressive history loading**

```bash
git add \
  src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/static/app.css \
  tests/test_web_assets_smoke.py
git commit -m "feat: load older messages on scroll"
```

### Task 4: Run group-switch requests concurrently and cancel obsolete work

**Files:**
- Modify: `src/telegram_kol_research/static/app.js:1-10`
- Modify: `src/telegram_kol_research/static/app.js:611-650`
- Modify: `src/telegram_kol_research/static/app.js:1110-1200`
- Modify: `src/telegram_kol_research/static/app.js:1739-1770`
- Test: `tests/test_web_assets_smoke.py:860-930`

**Step 1: Write failing concurrency and cancellation assertions**

Add:

```python
def test_group_switch_starts_message_companion_before_strategy_wait_and_aborts_previous(
    tmp_path,
):
    js = TestClient(
        create_web_app(database_path=tmp_path / "research.db")
    ).get("/static/app.js").text

    assert "let activeGroupSwitchController = null;" in js
    assert "activeGroupSwitchController.abort();" in js
    assert "new AbortController()" in js
    assert "signal: controller.signal" in js

    bind_start = js.index("function bindGroupLinks")
    bind_end = js.index("\nasync function loadVisibleGroupDestination", bind_start)
    bind_block = js[bind_start:bind_end]
    assert bind_block.index("loadGroupDetailCompanion") < bind_block.index(
        "await loadVisibleGroupDestination"
    )
```

Update the existing companion test to require the request-ID guard and injected `detailPromise`; remove the redundant selected-group assertion.

**Step 2: Run the asset tests and verify they fail**

Run:

```bash
uv run pytest tests/test_web_assets_smoke.py -q
```

Expected: FAIL because no active controller exists and the companion starts after the strategy request.

**Step 3: Add fetch signal support**

Change the fetch helpers to accept optional request options:

```javascript
async function fetchDetailPanel(chatId, options = {}) {
  const url = `/groups/${chatId}/detail?_t=${Date.now()}`;
  const response = await fetch(url, {
    cache: 'no-store',
    signal: options.signal,
  });
  // Keep the existing response validation and fragment parsing.
}

async function fetchStrategyMidPanel(chatId, filter, options = {}) {
  const url = `/groups/${chatId}/strategy-mid-panel?filter=${filter}&_t=${Date.now()}`;
  const response = await fetch(url, {
    cache: 'no-store',
    signal: options.signal,
  });
  // Keep the existing response validation and fragment parsing.
}
```

**Step 4: Introduce the active group-switch controller**

Add:

```javascript
let activeGroupSwitchController = null;

function beginGroupSwitchRequest() {
  if (activeGroupSwitchController) activeGroupSwitchController.abort();
  activeGroupSwitchController = new AbortController();
  return activeGroupSwitchController;
}
```

**Step 5: Start both requests before awaiting the strategy panel**

At the start of a group-link click:

```javascript
const requestId = ++groupSwitchRequestId;
const controller = beginGroupSwitchRequest();
const detailPromise = activeView === 'groups'
  ? fetchDetailPanel(chatId, { signal: controller.signal })
  : null;
const companionPromise = detailPromise
  ? loadGroupDetailCompanion({
      chatId,
      detailPanel,
      requestId,
      detailPromise,
    })
  : null;

await loadVisibleGroupDestination({
  activeView,
  chatId,
  filter,
  detailPanel,
  strategyPanel,
  requestId,
  signal: controller.signal,
});
```

Do not await `companionPromise` before committing the strategy panel. Attach a catch handler that ignores cancellation and reports only current-request failures through the existing status:

```javascript
if (companionPromise) {
  companionPromise.catch((error) => {
    if (error?.name === 'AbortError' || requestId !== groupSwitchRequestId) return;
    setAiStatus('消息加载失败，请重试。', true);
  });
}
```

Update `loadVisibleGroupDestination` to pass the signal to its fetch helper.

Update the companion to consume the already-started promise:

```javascript
async function loadGroupDetailCompanion({
  chatId,
  detailPanel,
  requestId,
  detailPromise,
}) {
  if (!detailPanel) return false;
  const nextContent = await detailPromise;
  if (requestId !== groupSwitchRequestId) return false;
  detailPanel.innerHTML = '';
  detailPanel.appendChild(nextContent);
  bindDetailPanelControls();
  bindWorkflowFilters();
  markWorkbenchLoaded('messages', chatId);
  return true;
}
```

The request-ID check remains authoritative. Do not require `getSelectedChatId() === chatId`, because the detail response may complete before the middle panel commits the new selection.

Apply the same controller creation when `loadSelectedGroupDestination` restores a saved group.

**Step 6: Run the asset tests**

Run:

```bash
uv run pytest tests/test_web_assets_smoke.py -q
```

Expected: PASS.

**Step 7: Commit concurrent group switching**

```bash
git add src/telegram_kol_research/static/app.js tests/test_web_assets_smoke.py
git commit -m "perf: parallelize cancellable group switching"
```

### Task 5: Run local regression and payload verification

**Files:**
- No production files expected

**Step 1: Run focused Web tests**

```bash
uv run pytest \
  tests/test_web_queries_messages.py \
  tests/test_web_group_messages_route.py \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py \
  -q
```

Expected: PASS.

**Step 2: Run the complete Web test set**

```bash
uv run pytest tests/test_web_*.py -q
```

Expected: PASS.

**Step 3: Run the full local suite**

```bash
uv run pytest -q
```

Expected: PASS, excluding only tests explicitly documented as requiring production credentials or server identity.

**Step 4: Compare initial response size**

Use a local database containing at least 21 messages for one group:

```bash
uv run python - <<'PY'
from pathlib import Path
from fastapi.testclient import TestClient
from telegram_kol_research.web_app import create_web_app

client = TestClient(create_web_app(database_path=Path("data/research.db")))
response = client.get("/groups/-1002344190971/detail")
print(response.status_code, len(response.content), response.text.count("data-message-card"))
PY
```

Expected: status 200, at most 20 message cards, and a materially smaller payload than the previous approximately 330 KB 50-message fragment on the same data.

**Step 5: Review the final diff**

```bash
git status --short
git diff --check
git diff --stat
```

Expected: only the scoped query, route, template, JavaScript, CSS, and test changes; no whitespace errors.

### Task 6: Push, deploy, and verify on the production server

**Files:**
- No additional source files expected

**Step 1: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: the remote branch advances to the reviewed local HEAD.

**Step 2: Update the production server**

Run the established helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls `codex/deepcoin-auto-trading-v1`, reinstalls the editable package, restarts `telegram-kol.service`, and reports an active service.

**Step 3: Check server-side route timings**

Inspect `web_perf` entries for:

- `/groups/{chat_id}/detail`;
- `/groups/{chat_id}/strategy-mid-panel`;
- a subsequent `/groups/{chat_id}/messages?before_message_id=...`.

Expected: initial `message_count=20`; pagination returns at most 20; no route errors.

**Step 4: Verify production browser behavior**

In the production execution console:

1. switch between two high-volume groups;
2. confirm the strategy and message panels begin updating without waiting serially;
3. confirm the initial panel contains at most 20 cards;
4. scroll near the bottom and confirm exactly one next-page request;
5. confirm older cards append below existing cards without a scroll jump;
6. switch groups rapidly and confirm an old response never overwrites the latest group;
7. apply text and sender filters and repeat pagination;
8. simulate or observe a pagination failure and confirm the retry control preserves existing cards.

Expected: all behaviors match the design and no trading or notification action is triggered by message browsing.

**Step 5: Confirm production safety**

```bash
systemctl is-active telegram-kol.service
```

Expected: `active`.

Confirm that Telegram monitoring, authoritative recognition, and Deepcoin execution logs show no regression caused by the Web-only change.
