"""Help articles render from the policy, chunk sensibly, and the retrieval metrics add up."""

import os

import pytest

from relay.domain.enums import ShipmentStatus
from relay.domain.policy_config import default_policy
from relay.kb.articles import load_articles, parse, render
from relay.kb.chunking import chunk_article
from relay.kb.embeddings import DIMENSIONS, HashingEmbedder, disable_onnxruntime_telemetry
from relay.kb.retrieval_eval import LabeledQuery, ModeScore, RetrievalReport, wilson_interval
from relay.kb.search import Mode, _excerpt, _fuse

POLICY = default_policy()


def test_there_are_30_articles_and_every_placeholder_renders() -> None:
    articles = load_articles(POLICY)
    assert len(articles) == 30
    assert len({article.slug for article in articles}) == 30
    for article in articles:
        assert "{{" not in article.body, article.slug
        assert article.title
        assert article.category


def test_articles_quote_the_live_policy_values() -> None:
    by_slug = {article.slug: article for article in load_articles(POLICY)}
    assert "within 30 days of delivery" in by_slug["return-policy"].body
    assert "within 15 days" in by_slug["return-policy"].body
    assert "$100.00" in by_slug["refund-reviews"].body
    assert "$50.00 or more" in by_slug["shipping-options"].body


def test_a_policy_change_changes_the_articles() -> None:
    changed = POLICY.model_copy(
        update={"returns": POLICY.returns.model_copy(update={"window_days": 45})}
    )
    article = next(a for a in load_articles(changed) if a.slug == "return-policy")
    assert "within 45 days of delivery" in article.body


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{returns.window_weeks}}", "unknown policy placeholder"),
        ("{{returns.window_days | shout}}", "unknown placeholder format"),
        ("{{ returns }}", "could not be parsed"),
    ],
)
def test_bad_placeholders_fail_loudly(template: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        render(template, POLICY)


def test_placeholder_formats() -> None:
    assert render("{{refunds.auto_approve_limit_cents | money}}", POLICY) == "$100.00"
    assert render("{{address_changes.approval_carrier_statuses | list}}", POLICY) == (
        f"{ShipmentStatus.LABEL_CREATED}, {ShipmentStatus.IN_TRANSIT}"
    )


def test_front_matter_is_required() -> None:
    with pytest.raises(ValueError, match="missing front matter"):
        parse("x", "no front matter", POLICY)
    with pytest.raises(ValueError, match="needs title and category"):
        parse("x", "---\ntitle: Only a title\n---\nBody", POLICY)


def test_chunks_follow_sections_and_carry_the_title() -> None:
    article = parse(
        "sample",
        "---\ntitle: Sample\ncategory: test\n---\nIntro.\n\n## First\nOne.\n\n## Second\nTwo.",
        POLICY,
    )
    chunks = chunk_article(article)
    assert [(c.heading, c.text) for c in chunks] == [
        ("Sample", "Intro."),
        ("Sample > First", "One."),
        ("Sample > Second", "Two."),
    ]


def test_long_sections_split_on_paragraphs() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(6))
    article = parse("long", f"---\ntitle: Long\ncategory: test\n---\n## Part\n{body}", POLICY)
    chunks = chunk_article(article, max_chars=500)
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 500 for chunk in chunks)
    assert {chunk.heading for chunk in chunks} == {"Long > Part"}


def test_hashing_embedder_is_normalized_and_deterministic() -> None:
    embedder = HashingEmbedder()
    first = embedder.embed_query("Refund my cracked headphones")
    assert len(first) == DIMENSIONS
    assert abs(sum(x * x for x in first) - 1) < 1e-9
    assert first == embedder.embed_documents(["refund my CRACKED headphones"])[0]


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    fused = _fuse([[("a", 9.0), ("b", 8.0)], [("b", 0.9), ("c", 0.8)]])
    assert [chunk for chunk, _ in fused] == ["b", "a", "c"]


def test_excerpts_are_trimmed_on_a_word_boundary() -> None:
    assert _excerpt("short text") == "short text"
    long = _excerpt("word " * 200, max_chars=50)
    assert long.endswith("…")
    assert len(long) <= 50


def test_wilson_interval_matches_known_values() -> None:
    low, high = wilson_interval(33, 40)
    assert (round(low, 3), round(high, 3)) == (0.680, 0.913)
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_retrieval_report_metrics() -> None:
    queries = [LabeledQuery(query=f"q{i}", article=f"a{i}") for i in range(4)]
    score = ModeScore(Mode.VECTOR, ranks=[1, 2, None, 5])
    assert score.recall_at_1 == 0.25
    assert score.recall_at_3 == 0.5
    assert score.mrr == pytest.approx((1 + 0.5 + 0.2) / 4)
    report = RetrievalReport(queries=queries, scores={Mode.VECTOR: score}, embedder="test")
    assert report.misses(Mode.VECTOR) == [("q2", "a2", None), ("q3", "a3", 5)]
    assert "| vector | 0.250 | 0.500 (" in report.markdown(command="relay kb eval")


def test_onnxruntime_telemetry_is_off_unless_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    disable_onnxruntime_telemetry()
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")  # an explicit choice is respected
    disable_onnxruntime_telemetry()
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "0"
