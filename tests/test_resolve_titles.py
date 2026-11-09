"""Title lookups when several Crossref records share the same title."""
from paperbot.resolve import Resolver


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.headers = {"Retry-After": "0"}  # no real waiting on 429 in tests

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, crossref_items, s2=None, s2_status=200):
        self.crossref_items, self.s2, self.s2_status = crossref_items, s2, s2_status

    def get(self, url, params=None, timeout=None, **kw):
        if "crossref" in url:
            return FakeResponse(200, {"message": {"items": self.crossref_items}})
        if "semanticscholar" in url:
            return FakeResponse(self.s2_status, {"data": [self.s2]} if self.s2 else {"data": []})
        raise AssertionError(url)


def rec(doi, title, cites=0):
    return {"DOI": doi, "title": [title], "is-referenced-by-count": cites, "type": "journal-article"}


TWINS = [rec("10.65215/mirror", "Attention Is All You Need", 0),
         rec("10.1201/chapter", "Attention Is All You Need", 3),
         rec("10.1007/other", "Is Attention All You Need?", 50)]


def test_single_exact_match_is_used():
    r = Resolver(FakeSession([rec("10.1109/cvpr.2016.90",
                                  "Deep Residual Learning for Image Recognition", 9000)]))
    assert r.by_title("Deep residual learning for image recognition").doi == "10.1109/cvpr.2016.90"


def test_duplicate_titles_are_settled_by_semantic_scholar():
    s2 = {"title": "Attention is All you Need", "year": 2017, "authors": [{"name": "A Vaswani"}],
          "externalIds": {"ArXiv": "1706.03762", "DOI": "10.48550/arXiv.1706.03762"}}
    p = Resolver(FakeSession(TWINS, s2)).by_title("Attention is all you need")
    assert p.arxiv_id == "1706.03762"


def test_duplicate_titles_without_semantic_scholar_take_the_most_cited():
    p = Resolver(FakeSession(TWINS, None, s2_status=429)).by_title("Attention is all you need")
    assert p.doi == "10.1201/chapter"  # never the reordered "Is Attention All You Need?"
