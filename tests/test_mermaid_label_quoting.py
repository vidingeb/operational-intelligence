"""Unquoted parentheses in Mermaid node labels are a total parse failure.

Observed live: the model answered a host-inventory question with

    esx01[esx01.vcf.local<br/>9.0.1 (Build 24957456)]

which mermaid-cli rejects with ``Expecting 'SQE' ... got 'PS'``. One such node
fails the whole diagram, so the reader gets no picture at all. Writing a build
or a version in parentheses is the natural way to write it, so prompting will
not remove this any more than it removed the trailing ``%%`` comments.

The risk in the repair is over-reach: ``[(...)]`` and ``([...])`` are *shapes*,
and quoting them would silently turn a cylinder into a rectangle. Those cases
are pinned here deliberately.
"""

import orchestrator as o


def fence(*body):
    return "\n".join(["```mermaid", *body, "```"])


def test_parenthesised_label_is_quoted():
    out = o.repair_mermaid(fence("graph TD", "  esx01[esx01 9.0.1 (Build 24957456)]"))
    assert 'esx01["esx01 9.0.1 (Build 24957456)"]' in out


def test_the_exact_line_that_failed_in_production():
    line = "        esx01[esx01.vcf.local<br/>9.0.1 (Build 24957456)]"
    out = o.repair_mermaid(fence("graph TD", "    subgraph ESXi Hosts", line))
    assert '"esx01.vcf.local<br/>9.0.1 (Build 24957456)"' in out


def test_label_without_parentheses_is_untouched():
    src = fence("graph TD", "  esx01[esx01.vcf.local]")
    assert o.repair_mermaid(src) == src


def test_already_quoted_label_is_not_double_quoted():
    src = fence("graph TD", '  esx01["esx01 (Build 1)"]')
    assert o.repair_mermaid(src) == src
    assert '""' not in o.repair_mermaid(src)


def test_cylinder_shape_is_not_rewritten():
    """``id[(Database)]`` is a cylinder; quoting it makes it a rectangle."""
    src = fence("graph TD", "  db1[(Datastore)]")
    assert o.repair_mermaid(src) == src


def test_stadium_shape_is_not_rewritten():
    src = fence("graph TD", "  s1([Start])")
    assert o.repair_mermaid(src) == src


def test_subroutine_shape_is_not_rewritten():
    src = fence("graph TD", "  s1[[Subroutine]]")
    assert o.repair_mermaid(src) == src


def test_hexagon_shape_is_not_rewritten():
    src = fence("graph TD", "  h1{{Hexagon}}")
    assert o.repair_mermaid(src) == src


def test_diamond_label_with_parentheses_is_quoted():
    out = o.repair_mermaid(fence("graph TD", "  d1{Full (95%)?}"))
    assert 'd1{"Full (95%)?"}' in out


def test_init_directive_is_never_rewritten():
    """``%%{init: {...}}%%`` is config, and rewriting its braces breaks it."""
    src = fence("%%{init: {'theme':'dark'}}%%", "graph TD", "  a[b]")
    assert o.repair_mermaid(src) == src


def test_multiple_labels_on_one_line_are_all_quoted():
    out = o.repair_mermaid(fence("graph TD", "  a[A (1)] --> b[B (2)]"))
    assert 'a["A (1)"]' in out and 'b["B (2)"]' in out


def test_trailing_comment_and_parentheses_are_both_repaired():
    out = o.repair_mermaid(fence("graph TD", "  a[A (1)] --> b %% note"))
    assert 'a["A (1)"]' in out
    assert "%% note" not in out


def test_prose_outside_the_fence_is_untouched():
    src = "Build 9.0.1 (24957456) is current.\n\n" + fence("graph TD", "  a[b]")
    assert src.split("\n\n")[0] in o.repair_mermaid(src)


def test_text_with_brackets_outside_a_mermaid_fence_is_untouched():
    src = "```python\nx = d[k (1)]\n```"
    assert o.repair_mermaid(src) == src
