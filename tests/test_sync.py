"""Working together: the lock kept in the mailbox and the records the computers share."""
import pytest

from fakes import FakeBox, FakeClock, FakeStore
from paperbot.db import DB, state_digest
from paperbot.sync import LEASE_S, Busy, Lease, LockLost, Sync, SyncError, pack_state


# ---- the lock ------------------------------------------------------------------------------
def lease(box, name, wrong_by=0.0, **kw):
    return Lease(FakeStore(box), f"{name}-abc123", FakeClock(box, wrong_by), **kw)


def test_only_one_computer_works_at_a_time():
    box = FakeBox()
    pc, laptop = lease(box, "OFFICE-PC"), lease(box, "LAPTOP")
    pc.acquire()
    assert pc.valid()
    with pytest.raises(Busy, match="PaperBot on OFFICE-PC is working right now"):
        laptop.acquire()
    assert not laptop.valid() and box.kinds() == ["lock"]     # the laptop left nothing behind
    pc.release()
    assert box.kinds() == []
    laptop.acquire()
    assert laptop.valid() and not pc.valid()


def test_two_computers_asking_at_the_same_moment():
    box = FakeBox()
    pc, laptop = lease(box, "PC"), lease(box, "LAPTOP")
    said = []

    def meanwhile():            # the laptop asks while the PC is waiting to see who was first
        with pytest.raises(Busy) as busy:
            laptop.acquire()
        said.append(str(busy.value))

    pc.clock.during_sleep = meanwhile
    pc.acquire()
    assert pc.valid() and not laptop.valid()
    assert "PaperBot on PC" in said[0] and box.kinds() == ["lock"]


def test_waiting_for_the_other_computer_to_finish():
    box = FakeBox()
    pc, laptop = lease(box, "PC"), lease(box, "LAPTOP")
    pc.acquire()
    naps = []
    plain_sleep = laptop.clock.sleep

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:              # 1st nap: seeing who was first; 2nd: waiting for the PC
            pc.release()
        plain_sleep(seconds)

    laptop.clock.sleep = sleep
    laptop.acquire(wait_s=60)
    assert laptop.valid() and len(naps) == 3


def test_a_computer_that_vanished_loses_the_lock_after_a_while():
    box = FakeBox()
    pc, laptop = lease(box, "PC"), lease(box, "LAPTOP")
    pc.acquire()
    box.now += LEASE_S - 60
    with pytest.raises(Busy):
        laptop.acquire()                                # not yet
    box.now += 120
    laptop.acquire()
    assert laptop.valid() and not pc.valid()
    assert box.kinds() == ["lock"]                      # the PC's old message was cleared away


def test_renewing_keeps_the_lock_through_long_work():
    box = FakeBox()
    pc, laptop = lease(box, "PC"), lease(box, "LAPTOP")
    pc.acquire()
    for _ in range(5):                                  # 50 minutes of work
        box.now += 600
        pc.beat()
        assert pc.valid()
        with pytest.raises(Busy):
            laptop.acquire()
    assert box.kinds() == ["beat", "lock"]              # old renewals don't pile up
    pc.release()
    assert box.kinds() == []


def test_a_computer_stops_trusting_its_lock_before_others_may_take_it():
    box = FakeBox()
    pc, laptop = lease(box, "PC"), lease(box, "LAPTOP")
    pc.acquire()
    box.now += LEASE_S - 100
    assert not pc.valid()                               # gave up with time to spare...
    with pytest.raises(Busy):
        laptop.acquire()                                # ...while nobody else can have it yet


def test_no_renewal_once_the_lock_has_run_out():
    box = FakeBox()
    pc, laptop = lease(box, "PC"), lease(box, "LAPTOP")
    pc.acquire()
    box.now += LEASE_S + 5
    laptop.acquire()                                    # took over
    saved = box.saved
    pc.beat()                                           # the PC wakes up and tries to renew
    assert box.saved == saved and not pc.valid() and laptop.valid()


def test_a_laptop_that_slept_knows_its_turn_is_over():
    box = FakeBox()
    laptop = lease(box, "LAPTOP")
    laptop.acquire()
    timer = laptop.clock.monotonic()
    laptop.clock.suspend(3600)                          # lid closed for an hour
    assert laptop.clock.monotonic() == timer            # its own timer didn't move...
    assert not laptop.valid()                           # ...but the date did


def test_renewal_notices_that_another_computer_took_over():
    box = FakeBox()
    pc = lease(box, "PC")
    pc.acquire()
    box.messages.clear()                                # someone cleared the PC's message away
    pc.beat()
    assert not pc.valid()


def test_wrong_clocks_do_not_matter_because_the_server_keeps_time():
    box = FakeBox()
    pc = lease(box, "PC")
    fast = lease(box, "FAST", wrong_by=+7200)           # two hours ahead
    pc.acquire()
    with pytest.raises(Busy):
        fast.acquire()                                  # its own clock says "long run out"
    box.now += LEASE_S + 5
    slow = lease(box, "SLOW", wrong_by=-7200)           # two hours behind
    slow.acquire()                                      # its own clock says "still held"
    assert slow.valid()


def test_a_crashed_turn_is_cleared_by_the_same_computer_at_once():
    box = FakeBox()
    notes = []
    pc = lease(box, "PC", remember=notes.append)
    pc.acquire()                                        # ...and then the program was killed
    again = lease(box, "PC")
    with pytest.raises(Busy):
        again.acquire()                                 # looks like somebody is working
    again.acquire(stale=notes[0])
    assert again.valid() and box.kinds() == ["lock"]


# ---- the shared records ----------------------------------------------------------------------
def computer(tmp_path, box, name, **kw):
    home = tmp_path / name
    return Sync(DB(home / "paperbot.db"), FakeStore(box), name, home / "sync.lock",
                clock=FakeClock(box), heartbeat=False,
                backup_path=home / "paperbot.before-sharing.db", **kw)


def same_records(*computers):
    return len({state_digest(c.db.export_state()) for c in computers}) == 1


def test_nothing_is_written_to_the_mailbox_until_sharing_is_started(tmp_path):
    box = FakeBox()
    pc = computer(tmp_path, box, "PC")
    pc.refresh()
    with pc.session():
        pc.db.add_person("asha@uni.edu", "Asha", "approved")
        with pc.session():                              # e.g. decide() inside run_once()
            pc.guard()
            pc.checkpoint()
    assert box.messages is None and box.saved == 0 and not pc.joined


def test_start_sharing_and_the_second_computer_joins_by_itself(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "OFFICE-PC"), computer(tmp_path, box, "LAPTOP")
    pc.db.add_person("asha@uni.edu", "Asha", "approved")
    rid = pc.db.add_request("<1@pc>", "asha@uni.edu", "Asha", "Paper request", "", "done")
    pc.db.update_item(pc.db.add_item(rid, "10.1/a", "doi", "10.1/a"), status="sent")
    # the laptop was used on its own before: it has history of its own
    laptop.db.add_person("ravi@uni.edu", "Ravi", "approved")
    for n in (1, 2):
        rid = laptop.db.add_request(f"<{n}@laptop>", "ravi@uni.edu", "Ravi", "Paper request",
                                    "", "queued")
        laptop.db.add_item(rid, f"10.2/{n}", "doi", f"10.2/{n}")
    laptop.db.mark_seen("<odd@laptop>", "error")
    laptop.db.log_download("publisher", "ravi@uni.edu")

    pc.start()
    assert pc.joined and box.kinds() == ["state"]
    laptop.refresh()                                    # nothing to press on the laptop
    assert laptop.joined and (tmp_path / "LAPTOP" / "paperbot.before-sharing.db").exists()
    pc.refresh()
    assert same_records(pc, laptop)
    db = pc.db
    assert {p["email"] for p in db.people()} == {"asha@uni.edu", "ravi@uni.edu"}
    assert [r["message_id"] for r in db.recent_requests()] == ["<2@laptop>", "<1@laptop>",
                                                               "<1@pc>"]
    assert [i["raw"] for i in db.active_items(("new",))] == ["10.2/1", "10.2/2"]
    assert db.is_seen("<odd@laptop>") and db.downloads_today() == 1
    assert sorted(pc.others() + laptop.others()) == ["LAPTOP", "OFFICE-PC"]
    assert box.kinds() == ["state", "state"]            # the newest and one spare; no locks


def test_what_one_computer_does_shows_up_on_the_other(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    pc.start()
    laptop.refresh()
    with laptop.session():
        laptop.db.add_person("asha@uni.edu", "Asha", "pending")
    assert pc.db.person("asha@uni.edu") is None
    pc.refresh()
    assert pc.db.person("asha@uni.edu")["status"] == "pending"
    with pc.session():
        pc.db.set_person_status("asha@uni.edu", "approved")
    with laptop.session():                              # a turn starts from the newest records
        assert laptop.db.person("asha@uni.edu")["status"] == "approved"
    saved = box.saved
    pc.refresh()
    laptop.refresh()
    with pc.session():
        pass
    assert box.saved == saved + 1 and box.kinds() == ["state", "state"]   # only the lock, gone again


def test_turns_do_not_overlap(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    pc.start()
    laptop.refresh()
    with pc.session():
        with pytest.raises(Busy, match="PaperBot on PC is working"):
            with laptop.session():
                raise AssertionError("two computers working at once")
    with laptop.session(wait_s=30):
        laptop.guard()


def test_cut_off_mid_turn_while_the_other_computer_carries_on(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    rid = pc.db.add_request("<1@x>", "asha@uni.edu", "Asha", "Paper request", "", "queued")
    paper = pc.db.add_item(rid, "10.1/x", "doi", "10.1/x")
    pc.start()
    laptop.refresh()
    with pytest.raises(LockLost):
        with pc.session():
            pc.db.log_download("publisher", "asha@uni.edu")
            pc.db.update_item(paper, status="sent", sent_at="2026-01-01T10:00:00", reported=1)
            pc.clock.suspend(3600)                      # the reply went out, then: lid closed
            pc.guard()
    with laptop.session():                              # the laptop did something else meanwhile
        laptop.db.add_person("new@uni.edu", "New", "approved")
        assert laptop.db.item(paper)["status"] == "new"     # it never heard about the reply
    with pc.session():                                  # the PC is back
        pass
    laptop.refresh()
    assert same_records(pc, laptop)
    for c in (pc, laptop):
        assert c.db.item(paper)["status"] == "sent"         # not sent a second time
        assert c.db.request(rid)["status"] == "done"
        assert c.db.downloads_today() == 1                  # still counted
        assert c.db.person("new@uni.edu")["status"] == "approved"


def test_unsaved_work_is_saved_at_the_next_turn_when_nobody_else_wrote(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    pc.start()
    laptop.refresh()
    with pytest.raises(OSError):
        with pc.session():
            pc.db.add_person("asha@uni.edu", "Asha", "approved")
            box.down = True                             # the network went away
    box.down = False
    laptop.refresh()
    assert laptop.db.person("asha@uni.edu") is None
    box.now += LEASE_S + 5                              # the PC couldn't even say it was done
    with pc.session():
        pass
    laptop.refresh()
    assert laptop.db.person("asha@uni.edu")["status"] == "approved"


def test_a_damaged_records_message_falls_back_to_the_one_before(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    pc.db.add_person("asha@uni.edu", "Asha", "approved")
    pc.start()
    with pc.session():
        pc.db.add_person("ravi@uni.edu", "Ravi", "approved")
    newest = max(box.messages)
    box.messages[newest] = (box.messages[newest][0], b"not what was saved")
    laptop.refresh()
    assert laptop.joined and laptop.db.person("asha@uni.edu")
    with pc.session():                                  # the PC still knows what was lost
        pass
    laptop.refresh()
    assert same_records(pc, laptop) and laptop.db.person("ravi@uni.edu")


def test_records_from_a_newer_paperbot_are_left_alone(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    pc.start()
    state = pc.db.export_state()
    state["format"] = 99
    FakeStore(box).add("state", node=pc.node, version=50, digest=state_digest(state),
                       payload=pack_state(state))
    with pytest.raises(SyncError, match="update PaperBot here"):
        laptop.refresh()
    assert not laptop.joined


def test_leaving_and_the_last_computer_takes_the_records_away(tmp_path):
    box = FakeBox()
    pc, laptop = computer(tmp_path, box, "PC"), computer(tmp_path, box, "LAPTOP")
    pc.start()
    laptop.refresh()
    pc.refresh()
    assert pc.others() == ["LAPTOP"]
    laptop.leave()
    assert not laptop.joined and laptop.others() == []
    pc.refresh()
    assert pc.others() == [] and pc.joined
    saved = box.saved
    laptop.db.add_person("asha@uni.edu", "Asha", "approved")    # on its own again
    assert box.saved == saved
    pc.leave()
    assert box.kinds() == [] and not pc.joined
    pc.refresh()
    assert not pc.joined                                # nothing left to join


def test_the_mailbox_check_before_sharing(tmp_path):
    box = FakeBox()
    pc = computer(tmp_path, box, "PC")
    assert "can hold PaperBot's shared records" in pc.selftest()
    assert box.kinds() == []
    box.down = True
    with pytest.raises(OSError):
        pc.start()
    assert not pc.joined
