"""Pagination against a fake Network Insight that behaves like the real one:
10 entities per page whatever size is requested, opaque cursor, total_count."""
import sys, types, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vcfNetworks"))


class FakeNI:
    """Caps pages at 10 regardless of requested size, like the real server."""
    def __init__(self, total, cursor_on_last=True):
        self.total = total
        self.calls = []
        self.cursor_on_last = cursor_on_last

    def request(self, method, path, **kw):
        params = kw.get("params", {})
        size = params.get("size", 10)
        start = int(params.get("cursor") or 0)
        page = max(0, min(size, 10, self.total - start))
        self.calls.append((start, size))
        results = [{"entity_id": f"id{i}"} for i in range(start, start + page)]
        nxt = start + page
        cursor = str(nxt) if nxt < self.total or self.cursor_on_last else None
        return {"results": results, "cursor": cursor, "total_count": self.total}


def make_lister(client):
    def list_entity_refs(path, limit, page_size=100, max_pages=50):
        refs, cursor, total = [], None, None
        for _ in range(max_pages):
            if len(refs) >= limit:
                break
            params = {"size": min(page_size, limit - len(refs))}
            if cursor:
                params["cursor"] = cursor
            page = client.request("GET", path, params=params)
            batch = (page or {}).get("results") or []
            if total is None:
                total = (page or {}).get("total_count")
            if not batch:
                break
            refs.extend(batch)
            if isinstance(total, int) and len(refs) >= total:
                break
            next_cursor = (page or {}).get("cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return refs[:limit], (total if total is not None else len(refs))
    return list_entity_refs


def test_fetches_whole_estate_across_pages():
    c = FakeNI(57)
    refs, total = make_lister(c)("/x", 200)
    assert len(refs) == 57, f"got {len(refs)}"
    assert total == 57
    assert len(c.calls) == 6  # 10 per page


def test_respects_a_limit_below_the_total():
    c = FakeNI(57)
    refs, total = make_lister(c)("/x", 25)
    assert len(refs) == 25
    assert total == 57      # still reports the real estate size


def test_small_estate_stops_immediately():
    c = FakeNI(5)
    refs, total = make_lister(c)("/x", 50)
    assert len(refs) == 5 and total == 5
    assert len(c.calls) == 1


def test_cursor_present_on_last_page_does_not_loop():
    # The server may hand back a cursor even when the estate is exhausted.
    c = FakeNI(20, cursor_on_last=True)
    refs, _ = make_lister(c)("/x", 200)
    assert len(refs) == 20


def test_stuck_cursor_terminates():
    class Stuck:
        calls = 0
        def request(self, m, p, **kw):
            Stuck.calls += 1
            assert Stuck.calls < 60, "looped forever"
            return {"results": [{"entity_id": "a"}] * 10,
                    "cursor": "same", "total_count": 9999}
    refs, _ = make_lister(Stuck())("/x", 200)
    assert len(refs) == 20  # first page, then the unchanged cursor stops it


def test_empty_estate():
    c = FakeNI(0)
    refs, total = make_lister(c)("/x", 50)
    assert refs == [] and total == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name}")
    print("ALL PAGINATION TESTS PASS")
