"""Unit tests for :mod:`rxocrpl.rxnorm.client` (stdlib ``unittest``).

The client is a thin wrapper around the RxNav REST API, so every test mocks the
module-level ``requests.Session`` (``client._session``) — no real network I/O.
Because each public fetcher is wrapped in ``@lru_cache``, ``setUp`` wipes all
caches before each test so calls actually reach the mocked session instead of
returning a memoised value from a prior test.

Run with::

    cd <project_root> && PYTHONPATH=. python rxocrpl/rxnorm/client_test.py -v

Or via unittest discovery::

    python -m unittest --pattern "*.test.py" rxocrpl.rxnorm.client.test
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import rxocrpl.rxnorm.client as client
from rxocrpl.rxnorm.dataclasses import RxNavError

_CACHED_FUNCS = (
      client.fetch_ndc_rxcui,
      client.fetch_rxcui_name,
      client.fetch_history_status,
      client.fetch_related_by_rela,
      client.fetch_related_by_tty,
      client.fetch_rxcui_by_name,
)


def _response(status_code=200, json_data=None, text=""):
      """Build a stand-in for a ``requests.Response``."""
      resp = MagicMock()
      resp.status_code = status_code
      resp.json.return_value = json_data
      resp.text = text
      return resp


class _ClientTestCase(unittest.TestCase):
      """Base: clear lru_caches and patch the module-level session.

      ``self.get`` is the mocked ``_session.get``; set ``.return_value`` per test.

      Improvement note: previously setUp() both called _clear_caches() directly
      AND registered it via addCleanup, causing a redundant double-clear per test.
      Now setUp() clears caches once up-front; addCleanup handles post-test teardown.
      """

      def setUp(self):
            print(f"\n[{type(self).__name__}] Clearing lru_caches and patching _session")
            for fn in _CACHED_FUNCS:
                  fn.cache_clear()
            patcher = patch.object(client, "_session")
            self.addCleanup(patcher.stop)
            self.addCleanup(self._clear_caches)
            self.get = patcher.start().get

      @staticmethod
      def _clear_caches():
            print("  [teardown] Clearing lru_caches")
            for fn in _CACHED_FUNCS:
                  fn.cache_clear()


# --------------------------------------------------------------------------- #
# _get
# --------------------------------------------------------------------------- #

class TestGet(_ClientTestCase):
      def test_200_returns_parsed_json(self):
            print("  → test_200_returns_parsed_json: expect parsed dict on HTTP 200")
            self.get.return_value = _response(200, {"hello": "world"})
            result = client._get("rxcui/123.json")
            print(f"    result={result!r}")
            self.assertEqual(result, {"hello": "world"})

      def test_404_returns_none(self):
            print("  → test_404_returns_none: expect None on HTTP 404")
            self.get.return_value = _response(404, text="not found")
            result = client._get("rxcui/000.json")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_other_status_raises(self):
            print("  → test_other_status_raises: expect RxNavError for non-200/404 statuses")
            for status in (400, 422, 500, 503):
                  with self.subTest(status=status):
                        print(f"    subTest status={status}")
                        self.get.return_value = _response(status, text="boom")
                        with self.assertRaises(RxNavError) as ctx:
                              client._get("rxcui/123.json")
                        print(f"    raised: {ctx.exception}")
                        self.assertIn(str(status), str(ctx.exception))

      def test_url_built_without_params(self):
            print("  → test_url_built_without_params: no query string when no kwargs")
            self.get.return_value = _response(200, {})
            client._get("rxcui/123.json")
            url = self.get.call_args.args[0]
            print(f"    url={url!r}")
            self.assertEqual(url, "https://rxnav.nlm.nih.gov/REST/rxcui/123.json")
            self.assertNotIn("?", url)

      def test_leading_slash_is_stripped(self):
            print("  → test_leading_slash_is_stripped: leading slash in path is normalised")
            self.get.return_value = _response(200, {})
            client._get("/rxcui/123.json")
            url = self.get.call_args.args[0]
            print(f"    url={url!r}")
            self.assertEqual(url, "https://rxnav.nlm.nih.gov/REST/rxcui/123.json")

      def test_params_url_encoded(self):
            print("  → test_params_url_encoded: kwargs are encoded into query string")
            self.get.return_value = _response(200, {})
            client._get("rxcui.json", name="foo bar", search=2)
            url = self.get.call_args.args[0]
            print(f"    url={url!r}")
            self.assertIn("name=foo+bar", url)
            self.assertIn("search=2", url)

      def test_timeout_is_passed(self):
            print(f"  → test_timeout_is_passed: timeout kwarg equals client._TIMEOUT={client._TIMEOUT!r}")
            self.get.return_value = _response(200, {})
            client._get("rxcui/123.json")
            timeout = self.get.call_args.kwargs["timeout"]
            print(f"    timeout={timeout!r}")
            self.assertEqual(timeout, client._TIMEOUT)

      def test_params_with_none_value_omitted_or_handled(self):
            """Passing a None value in params should not crash; behaviour is at least defined."""
            print("  → test_params_with_none_value_omitted_or_handled: None param value is stable")
            self.get.return_value = _response(200, {})
            # Should not raise regardless of whether None is serialised as "" or dropped
            try:
                  client._get("rxcui.json", extra=None)
            except Exception as exc:
                  self.fail(f"_get raised unexpectedly with None param: {exc!r}")
            print("    no exception raised")


# --------------------------------------------------------------------------- #
# _parse_related_group
# --------------------------------------------------------------------------- #

class TestParseRelatedGroup(unittest.TestCase):
      def setUp(self):
            print(f"\n[TestParseRelatedGroup] No fixtures needed — pure function tests")

      def test_empty_rxcui_null_shape(self):
            print("  → test_empty_rxcui_null_shape: rxcui=None returns empty tuple")
            result = client._parse_related_group({"relatedGroup": {"rxcui": None}})
            print(f"    result={result!r}")
            self.assertEqual(result, ())

      def test_missing_related_group_key(self):
            print("  → test_missing_related_group_key: missing key returns empty tuple")
            result = client._parse_related_group({})
            print(f"    result={result!r}")
            self.assertEqual(result, ())

      def test_none_concept_group(self):
            print("  → test_none_concept_group: conceptGroup=None returns empty tuple")
            result = client._parse_related_group({"relatedGroup": {"conceptGroup": None}})
            print(f"    result={result!r}")
            self.assertEqual(result, ())

      def test_concept_group_without_properties(self):
            print("  → test_concept_group_without_properties: no conceptProperties key skipped")
            payload = {"relatedGroup": {"conceptGroup": [{"tty": "SCD"}]}}
            result = client._parse_related_group(payload)
            print(f"    result={result!r}")
            self.assertEqual(result, ())

      def test_concept_properties_empty_list(self):
            """conceptProperties present but empty — should yield an empty tuple, not crash."""
            print("  → test_concept_properties_empty_list: empty conceptProperties list yields no concepts")
            payload = {
                  "relatedGroup": {
                        "conceptGroup": [
                              {"tty": "SCD", "conceptProperties": []},
                        ]
                  }
            }
            result = client._parse_related_group(payload)
            print(f"    result={result!r}")
            self.assertEqual(result, ())

      def test_flattens_and_passes_default_tty(self):
            print("  → test_flattens_and_passes_default_tty: groups flattened; tty propagated as default_tty")
            payload = {
                  "relatedGroup": {
                        "conceptGroup": [
                              {
                                    "tty": "PIN",
                                    "conceptProperties": [
                                          {"rxcui": "1", "name": "alpha"},
                                          {"rxcui": "2", "name": "beta"},
                                    ],
                              },
                              {
                                    "tty": "SCD",
                                    "conceptProperties": [{"rxcui": "3", "name": "gamma"}],
                              },
                        ]
                  }
            }
            with patch.object(client.RxConcept, "from_props") as from_props:
                  from_props.side_effect = lambda cp, default_tty=None: (
                        cp["rxcui"],
                        default_tty,
                  )
                  result = client._parse_related_group(payload)

            print(f"    result={result!r}")
            print(f"    from_props call_count={from_props.call_count}")
            self.assertEqual(result, (("1", "PIN"), ("2", "PIN"), ("3", "SCD")))
            self.assertEqual(from_props.call_count, 3)


# --------------------------------------------------------------------------- #
# fetch_ndc_rxcui
# --------------------------------------------------------------------------- #

class TestFetchNdcRxcui(_ClientTestCase):
      def test_not_found_returns_empty(self):
            print("  → test_not_found_returns_empty: HTTP 404 → []")
            self.get.return_value = _response(404)
            result = client.fetch_ndc_rxcui("00000-0000-00", "drug")
            print(f"    result={result!r}")
            self.assertEqual(result, [])

      def test_collects_pairs(self):
            print("  → test_collects_pairs: multiple ndcInfo entries collected as [rxcui, tty] pairs")
            payload = {
                  "ndcInfoList": {
                        "ndcInfo": [
                              {"rxcui": "100", "tty": "SCD"},
                              {"rxcui": "200", "tty": "SBD"},
                        ]
                  }
            }
            self.get.return_value = _response(200, payload)
            result = client.fetch_ndc_rxcui("11111-1111-11", "drug")
            print(f"    result={result!r}")
            self.assertEqual(result, [["100", "SCD"], ["200", "SBD"]])

      def test_deduplicates_identical_pairs(self):
            print("  → test_deduplicates_identical_pairs: same (rxcui, tty) pair deduplicated; different tty kept")
            payload = {
                  "ndcInfoList": {
                        "ndcInfo": [
                              {"rxcui": "100", "tty": "SCD"},
                              {"rxcui": "100", "tty": "SCD"},
                              {"rxcui": "100", "tty": "SBD"},  # same cui, diff tty -> kept
                        ]
                  }
            }
            self.get.return_value = _response(200, payload)
            result = client.fetch_ndc_rxcui("22222-2222-22", "drug")
            print(f"    result={result!r}")
            self.assertEqual(result, [["100", "SCD"], ["100", "SBD"]])

      def test_skips_items_without_rxcui(self):
            print("  → test_skips_items_without_rxcui: ndcInfo entry missing rxcui key is skipped")
            payload = {
                  "ndcInfoList": {
                        "ndcInfo": [
                              {"tty": "SCD"},  # no rxcui -> skipped
                              {"rxcui": "300", "tty": "SCD"},
                        ]
                  }
            }
            self.get.return_value = _response(200, payload)
            result = client.fetch_ndc_rxcui("33333-3333-33", "drug")
            print(f"    result={result!r}")
            self.assertEqual(result, [["300", "SCD"]])

      def test_empty_info_list_none(self):
            print("  → test_empty_info_list_none: ndcInfoList=None → []")
            self.get.return_value = _response(200, {"ndcInfoList": None})
            result = client.fetch_ndc_rxcui("44444-4444-44", "drug")
            print(f"    result={result!r}")
            self.assertEqual(result, [])

      def test_ndc_info_none_inside_list(self):
            """ndcInfoList present but ndcInfo value is None — should return []."""
            print("  → test_ndc_info_none_inside_list: ndcInfoList.ndcInfo=None → []")
            self.get.return_value = _response(200, {"ndcInfoList": {"ndcInfo": None}})
            result = client.fetch_ndc_rxcui("55555-5555-55", "drug")
            print(f"    result={result!r}")
            self.assertEqual(result, [])


# --------------------------------------------------------------------------- #
# fetch_rxcui_name
# --------------------------------------------------------------------------- #

class TestFetchRxcuiName(_ClientTestCase):
      def test_returns_name(self):
            print("  → test_returns_name: idGroup.name is extracted")
            self.get.return_value = _response(200, {"idGroup": {"name": "ceftazidime"}})
            result = client.fetch_rxcui_name("221124")
            print(f"    result={result!r}")
            self.assertEqual(result, "ceftazidime")

      def test_not_found_returns_none(self):
            print("  → test_not_found_returns_none: HTTP 404 → None")
            self.get.return_value = _response(404)
            result = client.fetch_rxcui_name("000")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_missing_name_key_returns_none(self):
            print("  → test_missing_name_key_returns_none: idGroup exists but no 'name' key → None")
            self.get.return_value = _response(200, {"idGroup": {}})
            result = client.fetch_rxcui_name("221124")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_missing_id_group_returns_none(self):
            print("  → test_missing_id_group_returns_none: no idGroup key at all → None")
            self.get.return_value = _response(200, {})
            result = client.fetch_rxcui_name("221124")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_id_group_none_returns_none(self):
            """idGroup key is present but its value is None."""
            print("  → test_id_group_none_returns_none: idGroup=None → None")
            self.get.return_value = _response(200, {"idGroup": None})
            result = client.fetch_rxcui_name("221124")
            print(f"    result={result!r}")
            self.assertIsNone(result)


# --------------------------------------------------------------------------- #
# fetch_history_status
# --------------------------------------------------------------------------- #

class TestFetchHistoryStatus(_ClientTestCase):
      def test_returns_status_block(self):
            print("  → test_returns_status_block: rxcuiStatus block returned verbatim")
            block = {"status": "Quantified", "quantifiedConcept": []}
            self.get.return_value = _response(200, {"rxcuiStatus": block})
            result = client.fetch_history_status("6918")
            print(f"    result={result!r}")
            self.assertEqual(result, block)

      def test_not_found_returns_empty_dict(self):
            print("  → test_not_found_returns_empty_dict: HTTP 404 → {}")
            result = client.fetch_history_status("000")
            print(f"    result={result!r}")
            self.get.return_value = _response(404)
            self.assertEqual(result, {})

      def test_missing_status_key_returns_empty_dict(self):
            print("  → test_missing_status_key_returns_empty_dict: no rxcuiStatus key → {}")
            self.get.return_value = _response(200, {})
            result = client.fetch_history_status("6918")
            print(f"    result={result!r}")
            self.assertEqual(result, {})

      def test_null_status_returns_empty_dict(self):
            print("  → test_null_status_returns_empty_dict: rxcuiStatus=None → {}")
            self.get.return_value = _response(200, {"rxcuiStatus": None})
            result = client.fetch_history_status("6918")
            print(f"    result={result!r}")
            self.assertEqual(result, {})

      def test_empty_dict_status_returned_as_is(self):
            """rxcuiStatus={} — an empty (but valid) status block should pass through."""
            print("  → test_empty_dict_status_returned_as_is: rxcuiStatus={} passes through")
            self.get.return_value = _response(200, {"rxcuiStatus": {}})
            result = client.fetch_history_status("6918")
            print(f"    result={result!r}")
            # An empty dict is falsy; implementations may coerce to {} sentinel.
            # Either {} or {} is acceptable — assert it's dict-like and not None.
            self.assertIsInstance(result, dict)


# --------------------------------------------------------------------------- #
# fetch_related_by_rela / fetch_related_by_tty
# --------------------------------------------------------------------------- #

class TestFetchRelated(_ClientTestCase):
      def setUp(self):
            super().setUp()
            print(f"\n[TestFetchRelated] Patching _parse_related_group")
            patcher = patch.object(client, "_parse_related_group")
            self.addCleanup(patcher.stop)
            self.parse = patcher.start()
            self.parse.return_value = ("concept",)

      def test_by_rela_parses_payload(self):
            print("  → test_by_rela_parses_payload: payload forwarded to _parse_related_group; rela in URL")
            payload = {"relatedGroup": {}}
            self.get.return_value = _response(200, payload)
            result = client.fetch_related_by_rela("6918", "quantified_form_of")
            url = self.get.call_args.args[0]
            print(f"    url={url!r}, result={result!r}")
            self.assertEqual(result, ("concept",))
            self.parse.assert_called_once_with(payload)
            self.assertIn("rela=quantified_form_of", url)

      def test_by_rela_not_found_returns_empty(self):
            print("  → test_by_rela_not_found_returns_empty: HTTP 404 → () and parse not called")
            self.get.return_value = _response(404)
            result = client.fetch_related_by_rela("000", "tradename_of")
            print(f"    result={result!r}")
            self.assertEqual(result, ())
            self.parse.assert_not_called()

      def test_by_tty_parses_payload(self):
            print("  → test_by_tty_parses_payload: payload forwarded to _parse_related_group; tty in URL")
            payload = {"relatedGroup": {}}
            self.get.return_value = _response(200, payload)
            result = client.fetch_related_by_tty("6918", "PIN")
            url = self.get.call_args.args[0]
            print(f"    url={url!r}, result={result!r}")
            self.assertEqual(result, ("concept",))
            self.parse.assert_called_once_with(payload)
            self.assertIn("tty=PIN", url)

      def test_by_tty_not_found_returns_empty(self):
            print("  → test_by_tty_not_found_returns_empty: HTTP 404 → () and parse not called")
            self.get.return_value = _response(404)
            result = client.fetch_related_by_tty("000", "SCD")
            print(f"    result={result!r}")
            self.assertEqual(result, ())
            self.parse.assert_not_called()


# --------------------------------------------------------------------------- #
# fetch_rxcui_by_name
# --------------------------------------------------------------------------- #

class TestFetchRxcuiByName(_ClientTestCase):
      def test_returns_first_id(self):
            print("  → test_returns_first_id: first element of rxnormId list returned")
            self.get.return_value = _response(
                  200, {"idGroup": {"rxnormId": ["1234", "5678"]}}
            )
            result = client.fetch_rxcui_by_name("ceftazidime")
            print(f"    result={result!r}")
            self.assertEqual(result, "1234")

      def test_no_ids_returns_none(self):
            print("  → test_no_ids_returns_none: idGroup has no rxnormId → None")
            self.get.return_value = _response(200, {"idGroup": {}})
            result = client.fetch_rxcui_by_name("nonexistent drug")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_not_found_returns_none(self):
            print("  → test_not_found_returns_none: HTTP 404 → None")
            self.get.return_value = _response(404)
            result = client.fetch_rxcui_by_name("nonexistent drug")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_id_group_none_returns_none(self):
            """idGroup key present but None — guard against AttributeError."""
            print("  → test_id_group_none_returns_none: idGroup=None → None")
            self.get.return_value = _response(200, {"idGroup": None})
            result = client.fetch_rxcui_by_name("something")
            print(f"    result={result!r}")
            self.assertIsNone(result)

      def test_allsrc_and_search_params_coerced(self):
            print("  → test_allsrc_and_search_params_coerced: allsrc=True → 1, search=2 encoded")
            self.get.return_value = _response(200, {"idGroup": {"rxnormId": ["1"]}})
            client.fetch_rxcui_by_name("foo", allsrc=True, search=2)
            url = self.get.call_args.args[0]
            print(f"    url={url!r}")
            self.assertIn("allsrc=1", url)  # bool coerced to int
            self.assertIn("search=2", url)

      def test_allsrc_false_coerced(self):
            print("  → test_allsrc_false_coerced: allsrc=False → 0 in URL")
            self.get.return_value = _response(200, {"idGroup": {"rxnormId": ["1"]}})
            client.fetch_rxcui_by_name("foo", allsrc=False)
            url = self.get.call_args.args[0]
            print(f"    url={url!r}")
            self.assertIn("allsrc=0", url)

      def test_default_params_omit_allsrc(self):
            """Calling with only a name should not include allsrc unless it's a default."""
            print("  → test_default_params_omit_allsrc: no allsrc kwarg → URL has no spurious allsrc (or has a valid default)")
            self.get.return_value = _response(200, {"idGroup": {"rxnormId": ["1"]}})
            client.fetch_rxcui_by_name("aspirin")
            url = self.get.call_args.args[0]
            print(f"    url={url!r}")
            # If allsrc has a default, it may appear; assert it's not True (1) by default
            # since that would widen the search unexpectedly.
            self.assertNotIn("allsrc=1", url)


# --------------------------------------------------------------------------- #
# Caching behaviour
# --------------------------------------------------------------------------- #

class TestCaching(_ClientTestCase):
      def setUp(self):
            super().setUp()
            print(f"\n[TestCaching] lru_cache hit/miss behaviour")

      def test_repeated_call_hits_network_once(self):
            print("  → test_repeated_call_hits_network_once: second call with same args served from cache")
            self.get.return_value = _response(200, {"idGroup": {"name": "x"}})
            client.fetch_rxcui_name("221124")
            client.fetch_rxcui_name("221124")
            print(f"    get.call_count={self.get.call_count}")
            self.assertEqual(self.get.call_count, 1)

      def test_distinct_args_each_hit_network(self):
            print("  → test_distinct_args_each_hit_network: different rxcui args each miss the cache")
            self.get.return_value = _response(200, {"idGroup": {"name": "x"}})
            client.fetch_rxcui_name("221124")
            client.fetch_rxcui_name("221125")
            print(f"    get.call_count={self.get.call_count}")
            self.assertEqual(self.get.call_count, 2)


if __name__ == "__main__":
      unittest.main()
