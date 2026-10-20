"""Offline tests of slide grounding, geometry, planning, and guarded writes."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from figurefinder import captions, citations, google_slides, slide_layout, slide_policy, slides


def element(oid="body", x=30, y=70, w=260, h=250, text="The chambers of the heart"):
    return {"objectId": oid, "size": {"width": {"magnitude": w, "unit": "PT"},
            "height": {"magnitude": h, "unit": "PT"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"},
            "shape": {"text": {"textElements": [{"textRun": {"content": text}}]}}}


def deck():
    return {"presentationId": "deck_123", "revisionId": "rev_1", "title": "Anatomy",
            "pageSize": {"width": {"magnitude": 720, "unit": "PT"},
                         "height": {"magnitude": 405, "unit": "PT"}},
            "slides": [{"objectId": "slide1", "pageElements": [element()]}]}


def decision():
    return {"needs_figure": True, "target_block_ids": ["body"], "reason": "Anatomy benefits from a view.",
            "figure_brief": "Heart chambers", "visual_style": "Labelled diagram"}


def plan():
    return {"version": 1, "presentation_id": "deck_123", "revision_id": "rev_1", "slides": [
        {"slide_id": "slide1", "status": "ready", "box": [350, 100, 250, 200], "decision": decision(),
         "figure": {"url": "https://example.org/heart.png", "source_page": "https://example.org/heart",
                    "license": "CC BY", "verdict": "Heart chambers"}}]}


CAPTION = "Figure 1: Heart chambers, a cutaway showing the chambers involved in circulation."


def captioned_plan():
    proposed = plan()
    proposed["version"] = 2
    row = proposed["slides"][0]
    row["caption"] = CAPTION
    row["box"], row["caption_box"] = slide_layout.fit_captioned_image(
        [330, 40, 350, 340], 1600, 1000, CAPTION)
    return proposed


def cited_plan():
    proposed = captioned_plan()
    proposed["version"] = 3
    row = proposed["slides"][0]
    row["figure"].update(title="Heart", creator="A. Author", license="CC BY 4.0")
    row["citation"] = citations.make_citation(row["figure"])
    row["box"], row["caption_box"], row["citation_box"] = slide_layout.fit_cited_image(
        [330, 40, 350, 340], 1600, 1000, CAPTION, row["citation"]["text"])
    return proposed


class CitationAndAlignmentTests(unittest.TestCase):
    def test_credit_uses_metadata_and_links_original_source_page(self):
        credit = citations.make_citation({"url": "https://cdn.example/image.jpg",
            "source_page": "https://museum.example/work/1", "creator": "<a>Alice &amp; Bob</a>",
            "title": "Anatomy", "license": "CC BY 4.0"})
        self.assertEqual(credit["url"], "https://museum.example/work/1")
        self.assertEqual(credit["text"], "Source: Alice & Bob · Anatomy · museum.example · CC BY 4.0")

    def test_missing_metadata_is_not_invented_and_invalid_source_falls_back(self):
        credit = citations.make_citation({"url": "https://cdn.example/image.jpg",
                                           "source_page": "javascript:alert(1)"})
        self.assertEqual(credit, {"text": "Source: cdn.example", "url": "https://cdn.example/image.jpg"})
        with self.assertRaises(ValueError):
            citations.make_citation({"url": "file:///tmp/image"})
        credit = citations.make_citation({"url": "https://example.org/image", "creator": "Author",
                                           "attribution": "Custom credit"})
        self.assertIn("Custom credit", credit["text"])
        self.assertNotIn("Author", credit["text"])

    def test_portrait_and_landscape_images_share_edges_with_text_and_keep_aspect(self):
        for iw, ih in ((1600, 900), (900, 1600)):
            with self.subTest(dimensions=(iw, ih)):
                image, caption, credit = slide_layout.fit_cited_image(
                    [30, 50, 500, 800], iw, ih, CAPTION, "Source: example.org")
                self.assertAlmostEqual(image[2]/image[3], iw/ih)
                self.assertEqual(image[1], 50)
                self.assertEqual([image[0], image[2]], [caption[0], caption[2]])
                self.assertEqual([image[0], image[2]], [credit[0], credit[2]])
                self.assertEqual(caption[1] - (image[1]+image[3]), 8)
                self.assertAlmostEqual(credit[1] - (caption[1]+caption[3]), 4)
                self.assertLessEqual(credit[1]+credit[3], 850)
        self.assertIsNone(slide_layout.fit_cited_image([0, 0, 300, 60], 1600, 900, CAPTION, "Source: example.org"))

    def test_preferred_slot_aligns_with_related_text(self):
        slots = slide_layout.aligned_slots([330, 20, 350, 360], [[30, 70, 260, 250]])
        self.assertEqual(slots[0], [330, 70, 350, 310])
        self.assertEqual(slots[1], [330, 20, 350, 360])

    def test_batch_contains_linked_credit_and_counts_one_figure(self):
        client, proposed = mock.Mock(), cited_plan()
        client.get.return_value = deck()
        client.update.return_value = {}
        self.assertEqual(slides.apply_plan(client, proposed)["inserted"], 1)
        requests = client.update.call_args.args[2]
        credit_id = slides.image_id("slide1") + "_citation"
        credit_text = next(r["insertText"]["text"] for r in requests if r.get("insertText", {}).get("objectId") == credit_id)
        self.assertEqual(" ".join(credit_text.split()), proposed["slides"][0]["citation"]["text"])
        style = next(r["updateTextStyle"]["style"] for r in requests if r.get("updateTextStyle", {}).get("objectId") == credit_id)
        self.assertEqual(style["link"]["url"], "https://example.org/heart")
        self.assertEqual(sum("createShape" in r for r in requests), 2)

    def test_bad_citation_or_misalignment_never_writes(self):
        for change in ("wrong_source", "missing", "off_slide", "misaligned", "too_short", "overlap"):
            client, proposed = mock.Mock(), cited_plan()
            row = proposed["slides"][0]
            if change == "wrong_source": row["citation"]["url"] = "https://unrelated.example"
            if change == "missing": del row["citation"]
            if change == "off_slide": row["citation_box"][1] = 400
            if change == "misaligned": row["citation_box"][0] += 1
            if change == "too_short": row["citation_box"][3] = 1
            current = deck()
            if change == "overlap":
                x, y, w, h = row["citation_box"]
                current["slides"][0]["pageElements"].append(element("footer", x=x, y=y, w=w, h=h))
            client.get.return_value = current
            with self.subTest(change=change), self.assertRaises(ValueError):
                slides.apply_plan(client, proposed)
            client.update.assert_not_called()

    def test_reapply_requires_all_three_elements(self):
        current = deck()
        oid = slides.image_id("slide1")
        current["slides"][0]["pageElements"].extend([{"objectId": oid}, {"objectId": oid + "_caption"}])
        with self.assertRaises(ValueError):
            slides.build_requests(cited_plan(), current)
        current["slides"][0]["pageElements"].append({"objectId": oid + "_citation"})
        current["revisionId"] = "after_apply"
        self.assertEqual(slides.build_requests(cited_plan(), current), [])


class CaptionTests(unittest.TestCase):
    def test_overlong_caption_is_rewritten_without_discarding_selected_image(self):
        provider = mock.Mock()
        provider.name = "openai"
        provider.structured.side_effect = [
            {"title": "Animal cell", "description": "Long explanation " * 20},
            {"title": "Animal cell", "description": "A labelled view of the organelles inside an animal cell."},
        ]
        with mock.patch.object(captions.llm, "image_block", return_value={"kind": "image"}):
            result = captions.generate(2, "selected.jpg", [], "English", provider)
        self.assertEqual(result, "Figure 2: Animal cell, A labelled view of the organelles inside an animal cell.")
        self.assertEqual(provider.structured.call_count, 2)
        self.assertIn("Rewrite more concisely", provider.structured.call_args.kwargs["system"])
        self.assertEqual(provider.structured.call_args_list[0].kwargs["blocks"],
                         provider.structured.call_args_list[1].kwargs["blocks"])

    def test_caption_uses_selected_image_and_exact_template(self):
        provider = mock.Mock()
        provider.name = "openai"
        provider.structured.return_value = {
            "title": " Heart chambers ",
            "description": "a cutaway showing the chambers involved in circulation.",
        }
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "selected.jpg"
            Image.new("RGB", (20, 20)).save(image)
            self.assertEqual(captions.generate(1, image, [{"text": "heart"}], "English", provider), CAPTION)
        blocks = provider.structured.call_args.kwargs["blocks"]
        self.assertEqual(blocks[1]["kind"], "image")
        self.assertIn("heart", blocks[0]["text"])

    def test_caption_rejects_empty_and_oversized_model_output(self):
        provider = mock.Mock()
        provider.name = "openai"
        for data in ({"title": "", "description": "content"},
                     {"title": "title", "description": "x" * 151}):
            provider.structured.return_value = data
            with mock.patch.object(captions.llm, "image_block", return_value={}), self.assertRaises(ValueError):
                captions.generate(1, "image", [], "English", provider)

    def test_numbering_continues_from_existing_captions_in_groups(self):
        current = deck()
        self.assertEqual(captions.next_number(current), 1)
        current["slides"][0]["pageElements"].append({"objectId": "group", "elementGroup": {
            "children": [element("old_caption", text="Figure 8: Existing title, context.")]}})
        self.assertEqual(captions.next_number(current), 9)

    def test_caption_reserves_space_and_crowded_slot_is_rejected(self):
        image, caption = slide_layout.fit_captioned_image([0, 0, 350, 340], 1600, 1000, CAPTION)
        self.assertGreater(caption[1], image[1]+image[3])
        self.assertLessEqual(caption[1]+caption[3], 340)
        self.assertFalse(slide_layout.overlaps(image, caption))
        self.assertIsNone(slide_layout.fit_captioned_image([0, 0, 160, 50], 1600, 1000, CAPTION))

    def test_image_and_caption_are_inserted_atomically_and_count_as_one_figure(self):
        client = mock.Mock()
        client.get.return_value = deck()
        client.update.return_value = {}
        self.assertEqual(slides.apply_plan(client, captioned_plan())["inserted"], 1)
        requests = client.update.call_args.args[2]
        text = next(r["insertText"]["text"] for r in requests if "insertText" in r)
        self.assertEqual(" ".join(text.split()), CAPTION)
        self.assertEqual(sum("createImage" in r for r in requests), 1)
        self.assertEqual(sum("createShape" in r for r in requests), 1)

    def test_invalid_caption_geometry_and_missing_caption_never_write(self):
        for change in ("overlap", "off_slide", "missing", "too_short", "nan"):
            proposed, client = captioned_plan(), mock.Mock()
            row = proposed["slides"][0]
            if change == "overlap": row["caption_box"] = [40, 280, 350, 120]
            if change == "off_slide": row["caption_box"][1] = 400
            if change == "missing": del row["caption"]
            if change == "too_short": row["caption_box"][3] = 1
            if change == "nan": row["caption_box"][0] = float("nan")
            client.get.return_value = deck()
            with self.subTest(change=change), self.assertRaises(ValueError):
                slides.apply_plan(client, proposed)
            client.update.assert_not_called()

    def test_caption_pair_is_idempotent_but_partial_pair_is_rejected(self):
        current = deck()
        oid = slides.image_id("slide1")
        current["slides"][0]["pageElements"].append({"objectId": oid})
        with self.assertRaisesRegex(ValueError, "part of a figure/caption pair"):
            slides.build_requests(captioned_plan(), current)
        current["slides"][0]["pageElements"].append({"objectId": oid + "_caption"})
        current["revisionId"] = "after_apply"
        self.assertEqual(slides.build_requests(captioned_plan(), current), [])


class LayoutTests(unittest.TestCase):
    def test_real_nephron_slide_slot_fits_image_caption_and_source(self):
        caption = ("Figure 2: Nephron structure, A labelled diagram showing the glomerulus, "
                   "Bowman's capsule, tubules, loop of Henle, and collecting duct in urine formation.")
        citation = "Source: Kidney and nephron diagram · vedantu.com"
        fitted = slide_layout.fit_cited_image([360, 22.5, 337.5, 360],
                                              1024, 1035, caption, citation, minimum_width=129.6)
        self.assertIsNotNone(fitted)
        self.assertGreaterEqual(fitted[0][3], 72.9)
        self.assertLessEqual(fitted[2][1] + fitted[2][3], 382.5)

    def test_text_wrapping_uses_font_width_and_handles_long_tokens(self):
        narrow = slide_layout.caption_lines("iiiiiiiiiiiiiiiiiiiiiiiiiiii", 120)
        wide = slide_layout.caption_lines("WWWWWWWWWWWWWWWWWWWWWWWWWWWW", 120)
        self.assertLess(len(narrow), len(wide))
        text = "Anatomical caption with readable labels and a verylongwordwithoutspaces"
        lines = slide_layout.caption_lines(text, 180)
        font = slide_layout._layout_font(12)
        self.assertTrue(all(font.getlength(line)/4*1.08 <= 168 for line in lines))

    def test_grouped_text_tables_and_affine_bounds(self):
        child = element(x=10, y=20, w=30, h=40)
        group = {"objectId": "group", "transform": {"scaleX": 2, "scaleY": 2,
                 "translateX": 100, "translateY": 50, "unit": "PT"},
                 "elementGroup": {"children": [child]}}
        presentation = deck()
        presentation["slides"][0]["pageElements"] = [group]
        self.assertEqual(slide_layout.blocks_on(presentation["slides"][0])[0]["id"], "body")
        self.assertEqual(slide_layout.slide_geometry(presentation, presentation["slides"][0]),
                         [[120, 90, 60, 80]])
        table = {"table": {"tableRows": [{"tableCells": [
            {"text": {"textElements": [{"textRun": {"content": "Cell"}}]}}]}]}}
        self.assertEqual(slide_layout.text_of(table), "Cell")

    def test_inherited_placeholder_geometry_and_emu(self):
        presentation = deck()
        parent = element("parent")
        parent["transform"]["translateX"] = 127000
        parent["transform"]["unit"] = "EMU"
        presentation["layouts"] = [{"objectId": "layout", "pageElements": [parent]}]
        child = presentation["slides"][0]["pageElements"][0]
        del child["size"]
        del child["transform"]
        child["shape"]["placeholder"] = {"parentObjectId": "parent"}
        box = slide_layout.slide_geometry(presentation, presentation["slides"][0])[0]
        self.assertEqual(box[0], 10)
        self.assertEqual(box[2], 260)

    def test_crowded_slide_skips_and_free_rectangle_does_not_overlap(self):
        self.assertIsNone(slide_layout.free_box(720, 405, [[0, 0, 720, 405]]))
        occupied = [[30, 70, 260, 250]]
        box = slide_layout.free_box(720, 405, occupied)
        self.assertIsNotNone(box)
        self.assertFalse(slide_layout.overlaps(box, occupied[0]))
        fitted = slide_layout.fit_image(box, 1600, 900)
        self.assertAlmostEqual(fitted[2]/fitted[3], 1600/900)

    def test_unresolved_geometry_is_not_treated_as_free_space(self):
        presentation = deck()
        del presentation["slides"][0]["pageElements"][0]["transform"]
        with self.assertRaisesRegex(ValueError, "Cannot resolve"):
            slide_layout.slide_geometry(presentation, presentation["slides"][0])


class GuardedWriteTests(unittest.TestCase):
    def test_insert_and_attribution_are_one_revision_guarded_batch(self):
        client = mock.Mock()
        client.get.return_value = deck()
        client.update.return_value = {}
        self.assertEqual(slides.apply_plan(client, plan())["inserted"], 1)
        deck_id, revision, requests = client.update.call_args.args
        self.assertEqual((deck_id, revision), ("deck_123", "rev_1"))
        self.assertEqual(requests[0]["createImage"]["objectId"], slides.image_id("slide1"))
        self.assertIn("https://example.org/heart", requests[1]["updatePageElementAltText"]["description"])
        self.assertEqual(len(requests), 2)

    def test_stale_deck_wrong_deck_and_overlaps_never_write(self):
        for change in ("revision", "deck", "overlap", "nan", "off_slide"):
            client, proposed, current = mock.Mock(), plan(), deck()
            if change == "revision": current["revisionId"] = "new_revision"
            if change == "deck": current["presentationId"] = "another_deck"
            if change == "overlap": proposed["slides"][0]["box"] = [40, 90, 200, 180]
            if change == "nan": proposed["slides"][0]["box"][0] = float("nan")
            if change == "off_slide": proposed["slides"][0]["box"][0] = 710
            client.get.return_value = current
            with self.subTest(change=change), self.assertRaises(ValueError):
                slides.apply_plan(client, proposed)
            client.update.assert_not_called()

    def test_repeat_apply_and_empty_plan_are_noops(self):
        current, proposed = deck(), plan()
        current["revisionId"] = "after_apply"
        current["slides"][0]["pageElements"].append({"objectId": slides.image_id("slide1")})
        self.assertEqual(slides.build_requests(proposed, current), [])
        proposed["slides"][0]["status"] = "skipped"
        self.assertEqual(slides.build_requests(proposed, current), [])

    def test_http_write_carries_required_revision(self):
        session = mock.Mock()
        session.request.return_value.ok = True
        client = google_slides.SlidesClient(session)
        client.update("deck", "revision", [{"createImage": {}}])
        self.assertEqual(session.request.call_args.kwargs["json"]["writeControl"],
                         {"requiredRevisionId": "revision"})


class PolicyAndWorkflowTests(unittest.TestCase):
    def test_layout_retries_with_compact_caption_for_real_animal_cell_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            current = deck()
            current["slides"][0]["pageElements"] = [
                element("title", x=24.5433, y=35.0413, w=670.9134, h=45.0945, text="Animal cells"),
                element("body", x=24.5433, y=90.7461, w=318.4252, h=269.0079)]
            client.get.return_value = current
            args = SimpleNamespace(out=directory, max_figures=5, audience="students", language="English")
            figure = {"url": "https://example.org/image.png", "width": 860, "height": 689,
                      "source_page": "https://www.vedantu.com/biology/well-labelled-diagram-of-animal-cell",
                      "title": "Animal Cell Diagram Labelled", "preview": "slide-1/images/image.jpg"}
            long_caption = ("Figure 1: Animal Cell Structure, A labelled diagram showing nucleus, mitochondria, "
                "rough and smooth endoplasmic reticulum, Golgi apparatus, membrane and cytoplasm in animal cells.")
            compact = "Figure 1: Animal cell, Labelled organelles show how an animal cell is organized."
            with mock.patch.object(slide_policy, "decide", return_value=decision()), \
                 mock.patch.object(slides, "find_figure", return_value=figure), \
                 mock.patch.object(captions, "generate", side_effect=[long_caption, compact]) as generate:
                result = slides.create_plan(client, "deck_123", mock.Mock(), args)
            self.assertEqual(result["slides"][0]["status"], "ready")
            self.assertEqual(result["slides"][0]["caption"], compact)
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(generate.call_args.kwargs["max_description_chars"], 70)

    def test_run_applies_automatically_but_plan_does_not(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(slides.cli, "_load_dotenv"), \
             mock.patch.object(slides.cli, "_require_env", return_value=0), \
             mock.patch.object(slides.llm, "get_provider"), \
             mock.patch.object(slides, "authorize"), \
             mock.patch.object(slides, "create_plan", return_value=plan()) as create, \
             mock.patch.object(slides, "apply_plan", return_value={"inserted": 1}) as apply, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(slides.main(["run", "test_deck", "--sources", "wikimedia", "--out", directory]), 0)
            apply.assert_called_once()
            self.assertEqual(apply.call_args.args[1], create.return_value)
            self.assertEqual(json.loads((Path(directory)/"applied.json").read_text())["inserted"], 1)
            apply.reset_mock()
            self.assertEqual(slides.main(["plan", "test_deck", "--sources", "wikimedia", "--out", directory]), 0)
            apply.assert_not_called()

    def test_cli_uses_env_deck_and_allows_explicit_override(self):
        with mock.patch.object(slides.cli, "_load_dotenv"), \
             mock.patch.dict(os.environ, {"FF_GOOGLE_PRESENTATION": "https://docs.google.com/presentation/d/env_deck/edit"}), \
             mock.patch.object(slides.cli, "_require_env", return_value=0), \
             mock.patch.object(slides.llm, "get_provider"), \
             mock.patch.object(slides, "authorize"), \
             mock.patch.object(slides, "create_plan", return_value={"slides": []}) as create, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(slides.main(["plan", "--sources", "wikimedia"]), 0)
            self.assertEqual(create.call_args.args[1], "env_deck")
            self.assertEqual(slides.main(["plan", "override_deck", "--sources", "wikimedia"]), 0)
            self.assertEqual(create.call_args.args[1], "override_deck")

    def test_cli_missing_deck_explains_env_setting_before_authorization(self):
        with mock.patch.object(slides.cli, "_load_dotenv"), \
             mock.patch.dict(os.environ, {"FF_GOOGLE_PRESENTATION": ""}), \
             mock.patch.object(slides, "authorize") as authorize, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(slides.main(["plan"]), 1)
            self.assertIn("FF_GOOGLE_PRESENTATION", error.getvalue())
            authorize.assert_not_called()

    def test_policy_requires_real_target_ids_and_brief(self):
        provider = mock.Mock(name="provider")
        provider.name = "openai"
        for response in (dict(decision(), target_block_ids=["invented"]),
                         dict(decision(), figure_brief="")):
            provider.structured.return_value = response
            with mock.patch.object(slide_policy.llm, "image_block", return_value={}), self.assertRaises(ValueError):
                slide_policy.decide("context", "screenshot", {"body"}, provider)

    def test_plan_then_apply_without_external_services(self):
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.get.return_value = deck()
            client.update.return_value = {}
            args = SimpleNamespace(out=directory, max_figures=5, audience="students", language="English")
            figure = {"url": "https://example.org/heart.png", "width": 1600, "height": 1000,
                      "preview": "slide-1/images/heart.jpg"}
            with mock.patch.object(slide_policy, "decide", return_value=decision()), \
                 mock.patch.object(slides, "find_figure", return_value=figure), \
                 mock.patch.object(captions, "generate", return_value=CAPTION):
                result = slides.create_plan(client, "deck_123", mock.Mock(), args)
            client.update.assert_not_called()
            self.assertEqual(result["slides"][0]["status"], "ready")
            self.assertEqual(result["slides"][0]["target_blocks"][0]["id"], "body")
            self.assertEqual(result["slides"][0]["caption"], CAPTION)
            self.assertEqual(json.loads((Path(directory)/"plan.json").read_text())["policy_version"],
                             slide_policy.POLICY_VERSION)
            self.assertTrue((Path(directory)/"review.html").is_file())
            self.assertIn(CAPTION, (Path(directory)/"review.html").read_text())
            self.assertEqual(slides.apply_plan(client, result)["inserted"], 1)

    def test_slide_error_is_recorded_and_next_slide_still_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            current = deck()
            second = copy.deepcopy(current["slides"][0])
            second["objectId"] = "slide2"
            current["slides"].append(second)
            client.get.return_value = current
            args = SimpleNamespace(out=directory, max_figures=5, audience="students", language="English")
            skip = dict(decision(), needs_figure=False, target_block_ids=[], figure_brief="")
            with mock.patch.object(slide_policy, "decide", side_effect=[ValueError("bad AI output"), skip]):
                result = slides.create_plan(client, "deck_123", mock.Mock(), args)
            self.assertEqual([row["status"] for row in result["slides"]], ["error", "skipped"])
            client.update.assert_not_called()

    def test_public_source_format_is_checked_before_insertion(self):
        for fmt in ("PNG", "WEBP"):
            data = io.BytesIO()
            Image.new("RGB", (20, 20)).save(data, fmt)
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.iter_content.return_value = [data.getvalue()]
            with mock.patch.object(google_slides.requests, "get", return_value=response):
                if fmt == "WEBP":
                    with self.assertRaisesRegex(ValueError, "PNG, JPEG, or GIF"):
                        google_slides.check_remote_image("https://example.org/image")
                else:
                    google_slides.check_remote_image("https://example.org/image")

    def test_url_parser_rejects_non_slides_urls(self):
        self.assertEqual(google_slides.presentation_id("https://docs.google.com/presentation/d/abc-123/edit#slide=id.x"), "abc-123")
        with self.assertRaises(ValueError):
            google_slides.presentation_id("https://example.org/abc")


if __name__ == "__main__":
    unittest.main()
