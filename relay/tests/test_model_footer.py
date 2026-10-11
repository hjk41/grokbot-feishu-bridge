#!/usr/bin/env python3
"""Model footer tests — official-routing truthful semantics (no Statsig-as-model)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)

import model_resolve  # noqa: E402
import progress  # noqa: E402


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.patch_log = []
        self.lock = threading.Lock()
        self._n = 0

    def runner(self, argv, stdin_text=None):
        with self.lock:
            self.calls.append(list(argv))
            if "+messages-reply" in argv:
                self._n += 1
                return 0, json.dumps({"ok": True, "data": {"message_id": "om_prog_%04d" % self._n}}), ""
            if "messages" in argv and "patch" in argv:
                body = None
                for i, a in enumerate(argv):
                    if a == "--data":
                        body = argv[i + 1]
                self.patch_log.append(body)
                return 0, json.dumps({"ok": True, "data": {}}), ""
            return 0, json.dumps({"ok": True}), ""


def _el(card, eid):
    for e in (card.get("body") or {}).get("elements") or []:
        if e.get("element_id") == eid:
            return e
    return None


def _last_el(card):
    els = (card.get("body") or {}).get("elements") or []
    return els[-1] if els else None


def _card_text_blob(card):
    parts = []
    for e in (card.get("body") or {}).get("elements") or []:
        if isinstance(e.get("content"), str):
            parts.append(e["content"])
        # collapsible / nested
        for ne in e.get("elements") or []:
            if isinstance(ne.get("content"), str):
                parts.append(ne["content"])
    h = card.get("header") or {}
    for k in ("title", "subtitle"):
        t = (h.get(k) or {}).get("content")
        if t:
            parts.append(t)
    return "\n".join(parts)


def _assert_no_public_model(test, card, label=""):
    test.assertIsNone(_el(card, "model_footer"), label)
    ids = [e.get("element_id") for e in (card.get("body") or {}).get("elements") or []]
    test.assertNotIn("model_footer", ids, label)
    blob = _card_text_blob(card)
    for bad in ("模型：", "配置模型：", "官方路由", "model_footer"):
        test.assertNotIn(bad, blob, "%s leaked %r" % (label, bad))
    # status footer must remain
    test.assertIsNotNone(_el(card, "footer"), label)



OFFICIAL_FOOTER = "模型：自动选择（具体模型未公开）"


class TestModelResolve(unittest.TestCase):
    def test_auto_becomes_null_official_routing(self):
        for bad in ("Auto", "auto", "default", "auto-smart", "…", "...", "unknown"):
            r = model_resolve.resolve_model(override=bad)
            self.assertIsNone(r["model_id"], bad)
            self.assertIsNone(r["model_name"], bad)
            self.assertEqual(r["model_source"], "official-routing")
            self.assertNotIn("unknown", (r.get("model_name") or "").lower())
            self.assertEqual(model_resolve.model_footer_display(r), OFFICIAL_FOOTER)
            self.assertNotIn("官方路由", model_resolve.model_footer_display(r))

    def test_no_hardcoded_grok_45(self):
        r = model_resolve.resolve_model(existing={})
        self.assertIsNone(r["model_id"])
        self.assertIsNone(r["model_name"])
        self.assertEqual(r["model_source"], "official-routing")
        self.assertEqual(model_resolve.model_footer_display(r), OFFICIAL_FOOTER)
        disp = model_resolve.model_footer_display(r)
        self.assertNotIn("Grok 4.5", disp)
        self.assertNotIn("grok-4.5", disp.lower())
        self.assertNotIn("grok-4.5", (r.get("model_id") or "").lower())
        self.assertNotIn("grok-4.5", (r.get("model_name") or "").lower())

    def test_statsig_slots_ignored_in_default(self):
        """Statsig sand-* are routing config IDs — NOT models. Default stays unknown."""
        # Even if a leftover helper existed, resolve_model must not use Statsig.
        r = model_resolve.resolve_model(prefer_automation=True, existing={})
        self.assertIsNone(r["model_id"])
        self.assertIsNone(r["model_name"])
        self.assertEqual(r["model_source"], "official-routing")
        disp = model_resolve.model_footer_display(r)
        self.assertEqual(disp, OFFICIAL_FOOTER)
        self.assertNotIn("sand-automation", disp)
        self.assertNotIn("sand-default", disp)

    def test_manual_config_kind_forced(self):
        """--model always model_kind=config regardless of --model-kind/source."""
        r = model_resolve.resolve_model(
            override="sand-default",
            model_source="statsig",
            model_kind="runtime",  # must be ignored
        )
        self.assertEqual(r["model_kind"], "config")
        self.assertEqual(r["model_id"], "sand-default")
        disp = model_resolve.model_footer_display(r)
        self.assertEqual(disp, "配置模型：sand-default（实际模型未公开）")
        self.assertNotIn("官方路由", disp)

    def test_manual_config_escaping(self):
        r2 = model_resolve.resolve_model(override="my-model")
        self.assertEqual(r2["model_kind"], "config")
        self.assertEqual(
            model_resolve.model_footer_display(r2),
            "配置模型：my-model（实际模型未公开）",
        )
        # Malformed config model_id → truthful default (not raw @ junk)
        disp = model_resolve.model_footer_display(
            {"model_id": "evil@all", "model_kind": "config", "model_source": "explicit"},
            escape_fn=model_resolve.escape_model_label,
        )
        self.assertEqual(disp, OFFICIAL_FOOTER)
        self.assertNotIn("@all", disp)
        self.assertNotIn("evil", disp)
        seg = model_resolve.escape_model_label('hi <at>z</at> [l](http://e)')
        self.assertNotIn("<at", seg.lower())
        self.assertNotIn("](", seg)
        self.assertNotIn("http://", seg)

    def test_apply_model_to_state_ignores_env_when_frozen(self):
        """apply_model_to_state must preserve snapshot; FEISHU_PROGRESS_MODEL ignored."""
        state = {
            "model_id": None,
            "model_name": None,
            "model_source": "official-routing",
            "model_bound_to_task": True,
        }
        with mock.patch.dict(os.environ, {
            "FEISHU_PROGRESS_MODEL": "sand-automation",
            "FEISHU_MODEL_ID": "sand-default",
        }, clear=False):
            out = model_resolve.apply_model_to_state(state)
            # Must NOT call resolve that would pick env — snapshot preserved
            self.assertIsNone(out.get("model_id"))
            self.assertIsNone(out.get("model_name"))
            self.assertEqual(out.get("model_source"), "official-routing")
            self.assertNotEqual(out.get("model_kind"), "config")
            self.assertEqual(
                model_resolve.model_footer_display(out), OFFICIAL_FOOTER,
            )
            # resolve_model(existing=...) must also prefer frozen over env
            r = model_resolve.resolve_model(existing=state, refresh=False)
            self.assertIsNone(r.get("model_id"))
            self.assertEqual(r.get("model_source"), "official-routing")

    def test_sand_slot_never_runtime_from_turn(self):
        existing = {"resolved_model_id": "sand-automation"}
        r = model_resolve.resolve_model(existing=existing)
        self.assertIsNone(r["model_id"])
        self.assertEqual(r["model_source"], "official-routing")
        self.assertNotIn("sand-automation", model_resolve.model_footer_display(r))

    def test_concrete_turn_model_accepted(self):
        existing = {"resolved_model_id": "claude-sonnet-4"}
        r = model_resolve.resolve_model(existing=existing)
        self.assertEqual(r["model_id"], "claude-sonnet-4")
        self.assertEqual(r["model_kind"], "turn-resolved")
        self.assertEqual(
            model_resolve.model_footer_display(r),
            "模型：claude-sonnet-4",
        )

    def test_escape_at_and_links(self):
        dirty = {"model_id": "x", "model_name": 'x <at>@all</at> [a](http://e)',
                 "model_kind": "turn-resolved", "model_source": "task-turn"}
        seg = model_resolve.model_footer_display(dirty, escape_fn=progress.escape_lark_md)
        self.assertEqual(seg, "模型：x")
        seg2 = model_resolve.escape_model_label('hi <at>z</at> [l](http://e)')
        self.assertNotIn("<at", seg2.lower())
        self.assertNotIn("](", seg2)
        self.assertNotIn("http://", seg2)

    def test_no_proto_host_in_default(self):
        """Default path must not invent models from profile/host."""
        r = model_resolve.resolve_model(existing={})
        self.assertIsNone(r.get("model_id"))
        self.assertEqual(r["model_source"], "official-routing")


class TestProgressModelFooter(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        progress.TASKS_DIR = self._td.name
        self.fake = FakeFeishu()
        progress.set_runner(self.fake.runner)
        progress._heartbeats.clear()

    def tearDown(self):
        progress.set_runner(None)
        progress._heartbeats.clear()
        self._td.cleanup()

    def test_progress_omits_model_footer(self):
        card = progress.build_progress_card(
            task_id="mf0001aabbccddee", request_text="hi @x <at>y</at>",
            status="running", elapsed_sec=2,
            model_id=None, model_name=None,
            model_source="official-routing",
        )
        foot = _el(card, "footer")
        self.assertIsNotNone(foot)
        self.assertNotIn("sand-automation", foot["content"])
        self.assertNotIn("<at>", card["body"]["elements"][0]["content"])
        _assert_no_public_model(self, card, "progress-running")
        self.assertEqual(_last_el(card).get("element_id"), "footer")

    def test_summary_error_config_variants_omit_model(self):
        sc = progress.build_summary_card(
            task_id="mf0002aabbccddee", elapsed_sec=1, body_md="ok",
            model_id=None, model_name=None, model_source="official-routing",
        )
        _assert_no_public_model(self, sc, "summary-official")
        self.assertEqual(_last_el(sc).get("element_id"), "footer")

        ec = progress.build_error_card(
            task_id="mf0003aabbccddee", elapsed_sec=1, what="e",
            model_id=None, model_name=None, model_source="official-routing",
        )
        _assert_no_public_model(self, ec, "error-official")
        self.assertEqual(_last_el(ec).get("element_id"), "footer")

        sc2 = progress.build_summary_card(
            task_id="mf0004aabbccddee", elapsed_sec=1, body_md="ok",
            model_id="my-cfg", model_name="my-cfg",
            model_source="explicit", model_kind="config",
        )
        _assert_no_public_model(self, sc2, "summary-config")

        pc = progress.build_progress_card(
            task_id="mf0005aabbccddee", request_text="t", status="done",
            elapsed_sec=1, model_id="my-cfg", model_kind="config",
            model_source="explicit",
        )
        _assert_no_public_model(self, pc, "progress-done-config")

        ec2 = progress.build_error_card(
            task_id="mf0006aabbccddee", elapsed_sec=1, what="e",
            model_id="my-cfg", model_kind="config", model_source="explicit",
        )
        _assert_no_public_model(self, ec2, "error-config")

    def test_all_status_cards_omit_model(self):
        """active/waiting/done/failed/error (+ack/queued) never show model info."""
        variants = [
            ("ack", None, None, "official-routing", None),
            ("waiting", None, None, "official-routing", None),
            ("queued", None, None, "official-routing", None),
            ("running", "sand-default", "sand-default", "explicit", "config"),
            ("done", "my-cfg", "my-cfg", "explicit", "config"),
            ("failed", "claude-sonnet-4", "claude-sonnet-4", "turn", "runtime"),
        ]
        for status, mid, mname, src, kind in variants:
            card = progress.build_progress_card(
                task_id="mfstat01aabbccddee", request_text="t",
                status=status, elapsed_sec=1,
                model_id=mid, model_name=mname, model_source=src, model_kind=kind,
            )
            _assert_no_public_model(self, card, "progress-%s" % status)
        sc = progress.build_summary_card(
            task_id="mfstat02aabbccddee", elapsed_sec=1, body_md="done body",
            model_id="x", model_name="x", model_source="explicit", model_kind="config",
        )
        _assert_no_public_model(self, sc, "summary")
        ec = progress.build_error_card(
            task_id="mfstat03aabbccddee", elapsed_sec=1, what="boom", detail="d",
            model_id="x", model_name="x", model_source="explicit", model_kind="config",
        )
        _assert_no_public_model(self, ec, "error")

    def test_normal_unknown_despite_statsig_env(self):
        """Normal task stays official-routing even when Statsig bootstrap exists."""
        # Live Statsig file may exist on box; resolve must ignore it.
        st = progress.create_ack(
            message_id="om_null_1", chat_id="oc_n", chat_type="p2p",
            request_text="n", task_id="mfnull01aabbccdd",
        )
        self.assertIsNone(st.get("model_id"))
        self.assertIsNone(st.get("model_name"))
        self.assertEqual(st.get("model_source"), "official-routing")
        self.assertTrue(st.get("model_bound_to_task"))
        card = progress.build_progress_card(
            task_id=st["task_id"], request_text="n", status="ack", elapsed_sec=0,
            **progress._model_kwargs(st),
        )
        _assert_no_public_model(self, card, "ack-statsig")
        blob = _card_text_blob(card)
        self.assertNotIn("sand-automation", blob)
        self.assertNotIn("sand-default", blob)

    def test_unknown_snapshot_frozen_across_update(self):
        """Heartbeat/update/done must NOT re-resolve after official-routing snapshot."""
        st = progress.create_ack(
            message_id="om_freeze_1", chat_id="oc_f", chat_type="p2p",
            request_text="f", task_id="mffreeze1aabbccdd",
        )
        self.assertEqual(st["model_source"], "official-routing")
        self.assertIsNone(st["model_id"])

        with progress.task_guard(st["task_id"]):
            s = progress.load_task(st["task_id"])
            s["status"] = "running"
            s["webhook_ok"] = True
            s["bypass_fifo"] = True
            progress.save_task(s)

        # Env set to sand-* — must NOT rewrite frozen official-routing
        with mock.patch.dict(os.environ, {
            "FEISHU_PROGRESS_MODEL": "sand-automation",
            "FEISHU_MODEL_ID": "sand-default",
        }, clear=False):
            st2 = progress.update(st["task_id"], action="阶段A", detail="d")
        self.assertIsNone(st2.get("model_id"))
        self.assertIsNone(st2.get("model_name"))
        self.assertEqual(st2.get("model_source"), "official-routing")

        st3 = progress.done(st["task_id"], summary_md="sum", send_summary=True)
        self.assertIsNone(st3.get("model_id"))
        self.assertEqual(st3.get("model_source"), "official-routing")

        # Patched cards must omit model_footer and not leak sand-*
        for body in self.fake.patch_log:
            if not body:
                continue
            try:
                data = json.loads(body)
                content = data.get("content") or data
                if isinstance(content, str):
                    content = json.loads(content)
                card = content.get("card") or content
                if isinstance(card, dict):
                    _assert_no_public_model(self, card, "patched")
                    blob = json.dumps(card, ensure_ascii=False)
                    self.assertNotIn("sand-automation", blob)
                    self.assertNotIn("模型：", blob)
            except AssertionError:
                raise
            except Exception:
                pass

    def test_persist_and_override_with_config_kind(self):
        st = progress.create_ack(
            message_id="om_cfg_1", chat_id="oc_c", chat_type="p2p",
            request_text="c", task_id="mfcfg001aabbccdd",
            model="sand-default", model_source="explicit", model_kind="runtime",
        )
        # kind forced to config despite model_kind=runtime
        self.assertEqual(st["model_id"], "sand-default")
        self.assertEqual(st["model_kind"], "config")
        card0 = progress.build_progress_card(
            task_id=st["task_id"], request_text="c", status="ack", elapsed_sec=0,
            **progress._model_kwargs(st),
        )
        _assert_no_public_model(self, card0, "ack-config")
        # Internal display helper may still describe config; public card must not.
        self.assertEqual(
            model_resolve.model_footer_display(st),
            "配置模型：sand-default（实际模型未公开）",
        )

        with progress.task_guard(st["task_id"]):
            s = progress.load_task(st["task_id"])
            s["status"] = "running"
            s["webhook_ok"] = True
            s["bypass_fifo"] = True
            progress.save_task(s)
        st2 = progress.update(
            st["task_id"], action="x", model="my-override",
            model_source="whatever", model_kind="runtime",
        )
        self.assertEqual(st2["model_id"], "my-override")
        self.assertEqual(st2["model_kind"], "config")
        self.assertEqual(
            model_resolve.model_footer_display(st2),
            "配置模型：my-override（实际模型未公开）",
        )

    def test_malformed_config_model_id_all_card_builders(self):
        """Malformed model_id with model_kind=config → official footer on all cards."""
        bad = dict(
            model_id="evil@all <at>x</at> [a](http://e)",
            model_name="ignored-name",
            model_source="explicit",
            model_kind="config",
        )
        pc = progress.build_progress_card(
            task_id="mfbad01aabbccddee", request_text="t", status="running",
            elapsed_sec=1, **bad,
        )
        sc = progress.build_summary_card(
            task_id="mfbad02aabbccddee", elapsed_sec=1, body_md="ok", **bad,
        )
        ec = progress.build_error_card(
            task_id="mfbad03aabbccddee", elapsed_sec=1, what="e", **bad,
        )
        for card, label in ((pc, "progress"), (sc, "summary"), (ec, "error")):
            _assert_no_public_model(self, card, label)
            blob = json.dumps(card, ensure_ascii=False)
            self.assertNotIn("模型：", blob, label)
            self.assertNotIn("配置模型：", blob, label)
            self.assertNotIn("evil@all", blob, label)
            # Public card must not show model_footer content at all
            self.assertIsNone(_el(card, "model_footer"), label)

    def test_ensure_does_not_reresolve_official_routing(self):
        state = {
            "model_id": None,
            "model_name": None,
            "model_source": "official-routing",
            "model_bound_to_task": True,
        }
        with mock.patch.object(model_resolve, "resolve_model") as rm:
            progress._ensure_model_on_state(state)
            rm.assert_not_called()
        self.assertIsNone(state["model_id"])
        self.assertEqual(state["model_source"], "official-routing")

    def test_apply_model_to_state_direct_env_regression(self):
        """Direct apply_model_to_state (not only _ensure) ignores env on freeze."""
        st = progress.create_ack(
            message_id="om_apply_1", chat_id="oc_a", chat_type="p2p",
            request_text="a", task_id="mfapply1aabbccdd",
        )
        self.assertEqual(st["model_source"], "official-routing")
        self.assertIsNone(st["model_id"])
        with mock.patch.dict(os.environ, {
            "FEISHU_PROGRESS_MODEL": "sand-automation",
            "FEISHU_MODEL_ID": "hijack-model",
        }, clear=False):
            # Simulate heartbeat/update path calling apply without override
            model_resolve.apply_model_to_state(st)
            self.assertIsNone(st.get("model_id"))
            self.assertIsNone(st.get("model_name"))
            self.assertEqual(st.get("model_source"), "official-routing")
            self.assertNotIn("sand-automation", model_resolve.model_footer_display(st))
            # Also via _ensure (heartbeat)
            progress._ensure_model_on_state(st)
            self.assertIsNone(st.get("model_id"))
            self.assertEqual(st.get("model_source"), "official-routing")


    def test_model_footer_element_disabled(self):
        self.assertIsNone(model_resolve.model_footer_element({
            "model_id": None, "model_source": "official-routing",
        }))
        self.assertIsNone(model_resolve.model_footer_element({
            "model_id": "my-cfg", "model_kind": "config", "model_source": "explicit",
        }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
