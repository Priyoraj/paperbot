from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from paperbot.parse import clean_doi, item_from_url, parse_request, parse_text, strip_quoted


def kinds(items):
    return [(i.kind, i.value) for i in items]


def make_msg(subject, body, html=None):
    m = EmailMessage()
    m["From"] = "Asha Rao <asha@uni.edu>"
    m["Subject"] = subject
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return BytesParser(policy=policy.default).parsebytes(m.as_bytes())


def test_dois_with_trailing_punctuation_and_brackets():
    text = "Please send 10.1145/3292500.3330701. Also (10.1016/S0140-6736(20)30183-5), thanks"
    assert kinds(parse_text(text)) == [("doi", "10.1145/3292500.3330701"),
                                       ("doi", "10.1016/s0140-6736(20)30183-5")]


def test_doi_links_and_publisher_urls():
    assert item_from_url("https://doi.org/10.1038/nature14539").value == "10.1038/nature14539"
    assert item_from_url("https://dl.acm.org/doi/pdf/10.1145/3065386").value == "10.1145/3065386"
    assert item_from_url("https://onlinelibrary.wiley.com/doi/full/10.1002/anie.201915678"
                         ).value == "10.1002/anie.201915678"
    assert item_from_url("https://www.nature.com/articles/s41586-020-2649-2"
                         ).value == "10.1038/s41586-020-2649-2"
    assert item_from_url("https://link.springer.com/article/10.1007/s10994-021-06001-7"
                         ).value == "10.1007/s10994-021-06001-7"
    enc = item_from_url("https://example.org/action/show?doi=10.1080%2F01621459.2017.1285773")
    assert (enc.kind, enc.value) == ("doi", "10.1080/01621459.2017.1285773")


def test_arxiv_and_other_links():
    assert kinds(parse_text("arXiv:1706.03762v5 and https://arxiv.org/abs/2106.09685")) == [
        ("arxiv", "2106.09685"), ("arxiv", "1706.03762")]
    sd = item_from_url("https://www.sciencedirect.com/science/article/pii/S0893608014002135")
    assert sd.kind == "url"
    rg = item_from_url("https://www.researchgate.net/publication/123_Deep_Residual_Learning")
    assert (rg.kind, rg.value) == ("title", "Deep Residual Learning")


def test_titles_quoted_prefixed_and_bulleted():
    text = '''Hi Priya,
Could you send these?
1. Deep residual learning for image recognition
- Title: Attention is all you need
"Mastering the game of Go with deep neural networks and tree search"
Thanks!'''
    assert kinds(parse_text(text)) == [
        ("title", "Deep residual learning for image recognition"),
        ("title", "Attention is all you need"),
        ("title", "Mastering the game of Go with deep neural networks and tree search")]


def test_free_text_title_is_only_a_guess_and_chatter_is_ignored():
    text = "Hi Priya,\nCould you please send me this paper\nDeep learning for symbolic mathematics\nThanks a lot,\nRavi"
    assert kinds(parse_text(text)) == [("guess", "Deep learning for symbolic mathematics")]


def test_quoted_reply_text_is_ignored():
    body = ("Also this one: 10.1000/new.1\n\nOn Mon, 3 Nov 2026 at 10:00, Priya <p@x.com> wrote:\n"
            "> 10.1000/old.1 - attached\n")
    assert kinds(parse_text(strip_quoted(body))) == [("doi", "10.1000/new.1")]


def test_subject_carries_the_item():
    msg = make_msg("Paper request: Attention is all you need", "Hi Priya,\nThanks so much!\nAsha")
    assert kinds(parse_request(msg, ["paper request"], 10)) == [
        ("title", "Attention is all you need")]
    msg = make_msg("Paper request - 10.1145/3065386", "and 10.1038/nature14539 please")
    assert kinds(parse_request(msg, ["paper request"], 10)) == [
        ("doi", "10.1145/3065386"), ("doi", "10.1038/nature14539")]


def test_html_only_email_uses_hrefs_and_skips_blockquotes():
    m = EmailMessage()
    m["Subject"] = "Paper request"
    m.set_content('<div>Please send <a href="https://doi.org/10.1126/science.aaa8415">this</a>'
                  '</div><blockquote>10.9999/old</blockquote>', subtype="html")
    msg = BytesParser(policy=policy.default).parsebytes(m.as_bytes())
    assert kinds(parse_request(msg, ["paper request"], 10)) == [
        ("doi", "10.1126/science.aaa8415")]


def test_item_limit():
    text = "\n".join(f"10.1000/x{i}" for i in range(30))
    assert len(parse_text(text, limit=10)) == 10


def test_clean_doi():
    assert clean_doi("10.1002/ANIE.201915678/full") == "10.1002/anie.201915678"
    assert clean_doi("10.1000/abc?via=ihub") == "10.1000/abc"
