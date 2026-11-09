"""End-to-end runs of the bot against fake mail, lookup, VPN and publisher."""
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid

import pytest

from paperbot.config import Config
from paperbot.db import DB
from paperbot.mail import to_incoming
from paperbot.pipeline import Bot
from paperbot.publisher import FetchResult
from paperbot.resolve import Paper, ResolveError
from paperbot.sources import Candidate

OWNER = "priya@gmail.com"
PDF = b"%PDF-1.7\n" + b"x" * 2000


def email_msg(frm, subject, body, reply_to=None, extra=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = OWNER
    m["Subject"] = subject
    m["Message-ID"] = make_msgid(domain="test")
    if reply_to:
        m["Reply-To"] = reply_to
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return BytesParser(policy=policy.default).parsebytes(m.as_bytes())


class FakeMail:
    def __init__(self):
        self.inbox, self.sent = [], []

    def fetch(self, since_days, wanted):
        return [to_incoming(m) for m in self.inbox if wanted(m)]

    def send(self, msg):
        msg = BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
        self.sent.append(msg)
        if msg["To"] == OWNER:  # Gmail drops self-addressed mail into the inbox too
            self.inbox.append(msg)

    def to(self, addr):
        return [m for m in self.sent if m["To"] == addr]


class FakeResolver:
    def __init__(self, papers):
        self.papers = papers

    def resolve(self, kind, value):
        p = self.papers.get(value)
        if p is None:
            raise ResolveError("couldn't identify a paper with this exact title")
        return Paper(**p)


class FakeOA:
    def __init__(self, cands=None):
        self.cands = cands or {}

    def candidates(self, paper):
        return self.cands.get(paper.doi, [])

    def download(self, url):
        return PDF if "good" in url else None


class FakeVpn:
    def __init__(self):
        self.connected = False
        self.connects = self.disconnects = 0

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        self.connects += 1

    def disconnect(self):
        self.connected = False
        self.disconnects += 1


class FakePublisher:
    def __init__(self, results, vpn):
        self.results, self.vpn, self.fetched = results, vpn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def fetch(self, paper):
        assert self.vpn.connected, "publisher used without the VPN"
        self.fetched.append(paper.doi)
        return self.results.get(paper.doi, FetchResult("failed", reason="no PDF link"))


@pytest.fixture
def world(tmp_path):
    cfg = Config(owner_name="Priya", owner_email=OWNER, mail_username=OWNER,
                 path=tmp_path / "config.toml", max_papers_per_person_per_day=5,
                 auto_approve_domains=["college.edu"])
    mail, vpn = FakeMail(), FakeVpn()
    papers = {
        "10.1000/oa": dict(doi="10.1000/oa", title="Open paper", authors="Lee et al.", year=2020),
        "10.1000/pay": dict(doi="10.1000/pay", title="Paywalled paper", authors="Kim", year=2021),
        "10.1000/captcha": dict(doi="10.1000/captcha", title="Guarded paper", year=2019),
        "10.1000/green": dict(doi="10.1000/green", title="Green paper", year=2018),
    }
    oa = FakeOA({
        "10.1000/oa": [Candidate("https://good/oa.pdf", "publishedVersion", "unpaywall")],
        "10.1000/green": [Candidate("https://good/aam.pdf", "acceptedVersion", "unpaywall")],
    })
    pub = FakePublisher({"10.1000/pay": FetchResult("ok", PDF),
                         "10.1000/captcha": FetchResult("blocked", reason="captcha")}, vpn)
    bot = Bot(cfg, DB(tmp_path / "t.db"), mail, FakeResolver(papers), oa, vpn, lambda: pub)

    class W:
        pass
    w = W()
    w.cfg, w.mail, w.vpn, w.pub, w.bot = cfg, mail, vpn, pub, bot

    def run():
        Bot(cfg, w.bot.db, mail, FakeResolver(papers), oa, vpn, lambda: pub).run_once()
    w.run = run
    return w


def owner_reply(prompt, text):
    return email_msg(f"Priya <{OWNER}>", "Re: " + str(prompt["Subject"]),
                     f"{text}\n\nOn Mon, Priya wrote:\n> quoted prompt")


def test_new_person_needs_approval_then_gets_papers(world):
    w = world
    w.mail.inbox.append(email_msg("Asha <asha@uni.edu>", "Paper request",
                                  "Hi Priya,\n10.1000/oa\n10.1000/pay\nThanks", reply_to="x@evil.com"))
    w.run()
    assert w.mail.to("asha@uni.edu") == []               # nothing sent before approval
    prompts = w.mail.to(OWNER)
    assert len(prompts) == 1 and "[ref:" in prompts[0]["Subject"]
    assert "10.1000/pay" in prompts[0].get_body().get_content()
    assert w.vpn.connects == 0

    w.run()  # the bot's own prompt sits in the inbox now; it must be ignored
    assert len(w.mail.to(OWNER)) == 1

    w.mail.inbox.append(owner_reply(prompts[0], "Yes"))
    w.run()
    replies = w.mail.to("asha@uni.edu")
    assert len(replies) == 1                              # Reply-To was ignored
    assert w.mail.to("x@evil.com") == []
    r = replies[0]
    assert r["In-Reply-To"] and r["Subject"] == "Re: Paper request"
    names = [p.get_filename() for p in r.iter_attachments()]
    assert names == ["Lee 2020 - Open paper.pdf", "Kim 2021 - Paywalled paper.pdf"]
    assert w.pub.fetched == ["10.1000/pay"]                  # open-access one didn't need the VPN
    assert (w.vpn.connects, w.vpn.disconnects, w.vpn.connected) == (1, 1, False)
    assert w.bot.db.person("asha@uni.edu")["status"] == "approved"

    w.mail.inbox.append(email_msg("Asha <asha@uni.edu>", "Paper request", "10.1000/oa"))
    w.run()                                                # approved people: no new prompt
    assert len(w.mail.to("asha@uni.edu")) == 2
    assert not [m for m in w.mail.to(OWNER) if "Approve" in m["Subject"]][1:]


def test_owner_says_no(world):
    w = world
    w.mail.inbox.append(email_msg("spam@x.com", "Paper request", "10.1000/oa"))
    w.run()
    w.mail.inbox.append(owner_reply(w.mail.to(OWNER)[0], "no thanks"))
    w.run()
    assert w.bot.db.person("spam@x.com")["status"] == "denied"
    w.mail.inbox.append(email_msg("spam@x.com", "Paper request", "10.1000/pay"))
    w.run()
    assert w.mail.to("spam@x.com") == [] and w.vpn.connects == 0


def test_spoofed_approval_is_ignored(world):
    w = world
    w.mail.inbox.append(email_msg("eve@x.com", "Paper request", "10.1000/oa"))
    w.run()
    prompt = w.mail.to(OWNER)[0]
    fake = email_msg("eve@x.com", "Re: " + str(prompt["Subject"]), "yes")
    w.mail.inbox.append(fake)
    w.run()
    assert w.bot.db.person("eve@x.com")["status"] == "pending"
    assert w.mail.to("eve@x.com") == []


def test_approval_off_and_trusted_domain(world):
    w = world
    w.mail.inbox.append(email_msg("ravi@college.edu", "Paper request", "10.1000/oa"))
    w.run()
    assert len(w.mail.to("ravi@college.edu")) == 1        # trusted domain: no prompt
    w.cfg.require_approval = False
    w.mail.inbox.append(email_msg("new@else.org", "paper request please", "10.1000/oa"))
    w.run()
    assert len(w.mail.to("new@else.org")) == 1
    assert not any("Approve" in m["Subject"] for m in w.mail.to(OWNER))


def test_captcha_goes_to_owner_and_preprint_fallback(world):
    w = world
    w.bot.db.add_person("asha@uni.edu", "Asha", "approved")
    w.mail.inbox.append(email_msg("Asha <asha@uni.edu>", "Paper request",
                                  "10.1000/captcha\n10.1000/green\n\"Some unknown title here\""))
    w.run()
    reply = w.mail.to("asha@uni.edu")[0]
    body = reply.get_body(preferencelist=("plain",)).get_content()
    assert "Green paper" in body and "author's manuscript" in body
    assert "needs a person to download it; Priya will send it separately" in body
    assert "couldn't identify" in body
    assert len(list(reply.iter_attachments())) == 1
    alert = [m for m in w.mail.to(OWNER) if "need you" in m["Subject"]]
    assert len(alert) == 1 and "paperbot manual" in alert[0].get_body().get_content()
    assert w.vpn.connected is False

    class FakeManual:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def capture(self, paper, url):
            assert w.vpn.connected
            return PDF

    got = w.bot.run_manual(lambda: FakeManual(), say=lambda *a: None)
    assert got == 1
    assert w.bot.db.downloads_today() == 1  # a hand-fetched paper counts toward the ceiling
    follow_up = w.mail.to("asha@uni.edu")[-1]
    assert len(list(follow_up.iter_attachments())) == 1 and not w.vpn.connected
    w.run()
    assert len(w.mail.to("asha@uni.edu")) == 2            # nothing sent twice


def test_vpn_left_on_if_owner_had_it_on(world):
    w = world
    w.vpn.connected = True
    w.bot.db.add_person("asha@uni.edu", "Asha", "approved")
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "10.1000/pay"))
    w.run()
    assert w.vpn.connected and w.vpn.disconnects == 0
    assert len(w.mail.to("asha@uni.edu")) == 1


def test_thank_you_reply_gets_no_help_text_but_fresh_empty_request_does(world):
    w = world
    w.bot.db.add_person("asha@uni.edu", "Asha", "approved")
    w.mail.inbox.append(email_msg("asha@uni.edu", "Re: Paper request", "Thanks a lot!"))
    w.run()
    assert w.mail.to("asha@uni.edu") == []
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "Can you send it?"))
    w.run()
    assert "couldn't find a paper" in w.mail.to("asha@uni.edu")[0].get_body().get_content()


def body_of(msg):
    return msg.get_body(preferencelist=("plain",)).get_content()


def custom_limits(cfg, day, reserve, hour, person=5):
    cfg.use_recommended_limits = False
    cfg.max_papers_per_day, cfg.own_reserve_per_day = day, reserve
    cfg.max_papers_per_hour, cfg.max_papers_per_person_per_day = hour, person


def test_daily_limit_per_person_says_try_tomorrow(world):
    w = world
    w.bot.db.add_person("asha@uni.edu", "Asha", "approved")
    custom_limits(w.cfg, day=30, reserve=10, hour=10, person=1)
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "10.1000/oa\n10.1000/pay"))
    w.run()
    body = body_of(w.mail.to("asha@uni.edu")[0])
    assert "Open paper" in body and "attached" in body
    assert "today's limit of 1 paper - please send this one again tomorrow" in body
    assert w.vpn.connects == 0


def test_ceiling_asks_colleagues_to_try_tomorrow_but_free_copies_still_flow(world):
    w = world
    w.bot.db.add_person("asha@uni.edu", "Asha", "approved")
    custom_limits(w.cfg, day=3, reserve=2, hour=10)         # colleagues share just 1 a day
    w.bot.db.log_download("publisher", "ravi@college.edu")   # ...and it's already used
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "10.1000/oa\n10.1000/pay"))
    w.run()
    body = body_of(w.mail.to("asha@uni.edu")[0])
    assert "Open paper" in body and "attached" in body       # free copy doesn't count
    assert "today's download limit has been reached - please send this one again tomorrow" in body
    assert w.vpn.connects == 0 and w.pub.fetched == []
    alerts = [m for m in w.mail.to(OWNER) if "safe download limit" in m["Subject"]]
    assert len(alerts) == 1
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "10.1000/captcha"))
    w.run()
    assert "again tomorrow" in body_of(w.mail.to("asha@uni.edu")[1])
    assert len([m for m in w.mail.to(OWNER) if "safe download limit" in m["Subject"]]) == 1


def test_owner_can_use_the_reserve(world):
    w = world
    custom_limits(w.cfg, day=3, reserve=2, hour=10)
    w.bot.db.log_download("publisher", "ravi@college.edu")
    w.mail.inbox.append(email_msg(f"Priya <{OWNER}>", "Paper request", "10.1000/pay"))
    w.run()
    mine = [m for m in w.mail.to(OWNER) if m["Subject"] == "Re: Paper request"]
    assert len(mine) == 1 and len(list(mine[0].iter_attachments())) == 1
    assert w.pub.fetched == ["10.1000/pay"] and w.bot.db.downloads_today() == 2


def test_hourly_cap_waits_instead_of_refusing(world):
    w = world
    w.bot.db.add_person("asha@uni.edu", "Asha", "approved")
    custom_limits(w.cfg, day=30, reserve=10, hour=1)
    w.bot.db.log_download("publisher", "ravi@college.edu")
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "10.1000/pay"))
    w.run()
    assert w.mail.to("asha@uni.edu") == [] and w.vpn.connects == 0   # just waiting
    two_hours_ago = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    w.bot.db.conn.execute("UPDATE downloads SET at=?", (two_hours_ago,))
    w.bot.db.conn.commit()
    w.run()
    reply = w.mail.to("asha@uni.edu")
    assert len(reply) == 1 and len(list(reply[0].iter_attachments())) == 1


def test_recommended_limits_ignore_custom_numbers():
    cfg = Config(max_papers_per_day=1, own_reserve_per_day=0, max_papers_per_hour=99,
                 max_papers_per_person_per_day=50, publisher_delay_s=1)
    assert (cfg.daily_ceiling, cfg.own_reserve, cfg.colleague_ceiling, cfg.hourly_cap,
            cfg.per_person_cap, cfg.download_spacing_s) == (30, 10, 20, 10, 5, 20.0)
    cfg.use_recommended_limits = False
    assert (cfg.daily_ceiling, cfg.colleague_ceiling, cfg.hourly_cap) == (1, 1, 99)


def test_dmarc_failure_ignored(world):
    w = world
    w.mail.inbox.append(email_msg("asha@uni.edu", "Paper request", "10.1000/oa", extra={
        "Authentication-Results": "mx.google.com; spf=fail; dkim=none; dmarc=fail"}))
    w.run()
    assert w.mail.sent == []
