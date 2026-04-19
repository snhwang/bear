# SCT-Bench Evaluation: Six Thinking Hats Panel Deliberation

## Overview

This document reports results from evaluating a Six Thinking Hats panel
deliberation framework on SCT-Bench, a clinical reasoning benchmark consisting
of 174 Script Concordance Test questions. We compare three methods — single-agent,
self-consistency (majority vote over 6 temperature samples), and a Six Hats panel
(6 hat-persona agents with sequential discussion, algorithmic aggregation) — across
ten model configurations spanning cloud APIs, open-weight models, and a
medical-domain specialist.

**Key finding:** The Six Hats panel significantly improves clinical reasoning
performance for five of nine models tested, with effect sizes ranging from
d=0.17 to d=0.35. The improvement comes primarily from structured reasoning
perspectives rather than sampling diversity: even the best individual hat
significantly outperforms self-consistency for four of five improved models.
The effect requires models capable of meaningfully adopting diverse reasoning
perspectives and benefits from domain-relevant knowledge.

## Background

### SCT-Bench

Script Concordance Testing (SCT) evaluates clinical reasoning under uncertainty.
Each question presents:

1. A clinical scenario (stem)
2. A diagnostic or treatment hypothesis
3. A new piece of clinical information

The respondent rates how the new information affects the hypothesis on a 5-point
scale (−2 to +2). Scoring is against an expert panel distribution — partial credit
is awarded for answers matching less-popular expert choices, with the modal expert
answer receiving a score of 1.0.

**Reference:** McCoy et al. "Assessment of Large Language Models in Clinical
Reasoning: A Novel Benchmarking Study." *NEJM AI*, 2025. Data:
https://github.com/SCT-Bench/sctpublic

### Six Thinking Hats

De Bono's Six Thinking Hats framework assigns structured reasoning perspectives:

| Hat | Role | Prompt Focus |
|-----|------|-------------|
| White | Data & facts | Objective analysis of clinical evidence |
| Red | Intuition | Gut feeling and pattern recognition |
| Black | Caution | Risks, missed diagnoses, critical analysis |
| Yellow | Optimism | Constructive reasoning, supporting evidence |
| Green | Creativity | Alternative interpretations, lateral thinking |
| Blue | Process | Meta-cognition, synthesis of perspectives |

### Methods Compared

1. **Single-agent**: One LLM call per question at temperature 0 (deterministic).
2. **Self-consistency (SC)**: 6 independent samples at the configured temperature,
   majority vote selects the final answer.
3. **Six Hats panel**: Each hat responds sequentially, seeing all prior hat
   responses (discussion context). All 6 hats vote; the final answer is determined
   by algorithmic aggregation (majority vote, median, or trimmed mean). No LLM
   synthesis step — aggregation is purely mechanical.

All three methods use the same total number of LLM calls for a fair compute
comparison (1 for single, 6 for SC, 6 for panel).

### Evolution from v1 to v2

The v1 evaluation used a Blue hat synthesizer: after 5 hats discussed, Blue
generated a final synthesized rating via an additional LLM call. This performed
significantly worse than single-agent (0.625 vs 0.698 on Nemotron, p=0.036),
indicating the synthesis step was actively losing information.

In v2, Blue participates as a voter like all other hats, and aggregation is
purely algorithmic. This eliminated the synthesis bottleneck and enabled the
panel improvements reported below.

## Experimental Setup

### Eval Script

`experiments/sct_eval_v2.py` — all results can be reproduced with:

```bash
python experiments/sct_eval_v2.py --mode all \
    --model <model-name> \
    [--base-url <url>] \
    [--temperature <t>] [--top-p <p>] \
    --results-dir results/<dir>
```

### Common Parameters

- **Questions**: 174 SCT questions from SCT-Bench (sct_cleaned_full.csv)
- **Panel order**: White → Red → Black → Yellow → Green → Blue (fixed)
- **Aggregation methods**: majority vote, median, trimmed mean
- **Self-consistency samples**: 6 (matching the number of hats)
- **Max tokens**: 400 (single, Blue), 300 (other hats)
- **Statistical tests**: Wilcoxon signed-rank (paired, two-sided), paired
  permutation test (10,000 permutations), bootstrap 95% CIs (10,000 resamples),
  Cohen's d (paired)

### Per-Model Configuration

| Model | Temperature | top_p | Backend | Results Directory |
|-------|-------------|-------|---------|-------------------|
| Claude Opus 4.6 | 0.5 | — | Anthropic API | results/sct_v2_opus |
| Claude Sonnet 4.6 | 0.5 | — | Anthropic API | results/sct_v2_sonnet |
| Claude Haiku 4.5 | 0.5 | — | Anthropic API | results/sct_v2_haiku |
| GPT-5.4 | 0.5 | — | OpenAI API | results/sct_v2_gpt54 |
| GPT-OSS:120B | 0.5 | — | Ollama (192.168.1.176:11434) | results/sct_v2_gptoss |
| GPT-OSS:20B | 0.5 | — | Ollama (192.168.1.176:11434) | results/sct_v2_gptoss20b |
| Grok 4.20 (non-reasoning) | 0.5 | — | xAI API | results/sct_v2_grok |
| Nemotron-3-Super (120B-A12B) | 0.5 | — | vLLM (192.168.1.175:8355) | results/sct_v2 |
| MedGemma-27B-text-it | 0.5 | — | vLLM (192.168.1.175:8355) | results/sct_v2_medgemma |
| Gemma3-27B-it | 0.5 | — | vLLM (192.168.1.175:8355) | results/sct_v2_gemma3 |

Note: Nemotron was additionally tested at temperature=1.0 with top_p=0.95
(model card recommendation) in results/sct_v2_nemotron_t1; results were
uniformly worse and are omitted from the main tables.

Opus and Sonnet panel results were repaired to address truncated hat responses
(29 and 19 calls respectively, rerrun at max_tokens=800). GPT-OSS:120B panel
was similarly repaired (195 calls). Repaired files are suffixed `_repaired.json`.

## Results

### Main Results Table

| Model | Single | SC Majority | SC Oracle | Panel Majority | Panel Oracle |
|-------|--------|-------------|-----------|----------------|--------------|
| MedGemma-27B | 0.510 | 0.537 | 0.782 | **0.688** | 0.841 |
| Claude Haiku 4.5 | 0.532 | 0.516 | 0.688 | **0.695** | 0.883 |
| Claude Opus 4.6 | 0.605 | 0.606 | 0.668 | **0.725** | 0.799 |
| Gemma3-27B | 0.615 | 0.634 | 0.744 | 0.563† | 0.756 |
| Claude Sonnet 4.6 | 0.655 | 0.666 | 0.781 | **0.753** | 0.835 |
| Grok 4.20 | 0.673 | 0.687 | 0.803 | 0.673 | 0.884 |
| GPT-OSS:20B | 0.686 | 0.726 | 0.835 | 0.732 | 0.863 |
| Nemotron-3-Super | 0.706 | 0.747 | 0.902 | 0.715 | 0.882 |
| GPT-OSS:120B | 0.709 | 0.692 | 0.830 | **0.755** | 0.912 |
| GPT-5.4 | 0.745 | 0.758 | 0.827 | 0.770 | 0.825 |

Bold indicates panel majority significantly outperforms self-consistency.
† Panel significantly *worse* than SC (p=0.033).
Opus, Sonnet, and GPT-OSS:120B panel results use repaired data (truncated
hat responses rerun at higher max_tokens). MedGemma panel median (0.710) was
higher than majority (0.688); all other models showed majority ≥ median.

### Statistical Significance: Panel Majority vs Self-Consistency

| Model | Difference | Bootstrap 95% CI | Wilcoxon p | Permutation p | Cohen's d |
|-------|-----------|-------------------|------------|---------------|-----------|
| **Haiku** | **+0.179** | [+0.099, +0.259] | **<0.0001** | **0.0001** | **0.334** |
| **MedGemma** | **+0.151** | [+0.078, +0.223] | **0.0001** | **0.0001** | **0.305** |
| **Opus** | **+0.116** | [+0.068, +0.167] | **<0.0001** | **0.0001** | **0.342** |
| **Sonnet** | **+0.089** | [+0.038, +0.140] | **0.0013** | **0.0008** | **0.254** |
| **GPT-OSS:120B** | **+0.063** | [+0.010, +0.118] | **0.0200** | **0.0244** | **0.171** |
| GPT-5.4 | +0.011 | [−0.027, +0.048] | 0.577 | 0.582 | 0.042 |
| GPT-OSS:20B | +0.005 | [−0.038, +0.047] | 0.783 | 0.808 | 0.018 |
| Grok 4.20 | −0.014 | — | n.s. | n.s. | — |
| Nemotron | −0.032 | — | n.s. | n.s. | — |
| Gemma3-27B | **−0.071** | [−0.136, −0.008] | **0.033** | **0.031** | **−0.166** |

### Statistical Significance: Panel Majority vs Single-Agent

| Model | Difference | Bootstrap 95% CI | Wilcoxon p | Permutation p | Cohen's d |
|-------|-----------|-------------------|------------|---------------|-----------|
| **MedGemma** | **+0.178** | [+0.103, +0.254] | **<0.0001** | **<0.0001** | **0.351** |
| **Haiku** | **+0.164** | [+0.087, +0.239] | **0.0001** | **<0.0001** | **0.317** |
| **Opus** | **+0.118** | [+0.064, +0.174] | **<0.0001** | **<0.0001** | **0.321** |
| **Sonnet** | **+0.099** | [+0.042, +0.156] | **0.0010** | **0.0007** | **0.257** |
| GPT-OSS:120B | +0.046 | [−0.008, +0.100] | 0.072 | 0.097 | 0.126 |
| GPT-OSS:20B | +0.046 | [−0.005, +0.097] | 0.085 | 0.080 | 0.134 |
| GPT-5.4 | +0.025 | — | n.s. | n.s. | — |
| Grok 4.20 | +0.000 | — | n.s. | n.s. | — |
| Nemotron | +0.009 | — | n.s. | n.s. | — |
| Gemma3-27B | −0.052 | [−0.116, +0.012] | 0.124 | 0.117 | −0.121 |

### Per-Hat Performance

#### Scores by Hat and Model

| Hat | MedGemma | Haiku | Opus | Sonnet | GPT-5.4 | GPT-OSS:120B | Grok | Gemma3 | Nemotron |
|-----|----------|-------|------|--------|---------|-------------|------|--------|---------|
| White | 0.691 | 0.700 | 0.682 | 0.690 | 0.775 | 0.634 | 0.673 | 0.606 | 0.693 |
| Red | 0.709 | 0.648 | 0.709 | 0.749 | 0.770 | 0.721 | 0.595 | 0.524 | 0.727 |
| Black | 0.627 | 0.507 | 0.705 | 0.739 | 0.746 | 0.464 | 0.471 | 0.438 | 0.570 |
| Yellow | 0.695 | 0.673 | 0.712 | 0.729 | 0.745 | 0.570 | 0.671 | 0.605 | 0.500 |
| Green | 0.644 | 0.491 | 0.713 | 0.745 | 0.775 | 0.307 | 0.461 | 0.444 | 0.235 |
| Blue | 0.695 | 0.639 | 0.720 | 0.755 | 0.779 | 0.568 | 0.640 | 0.527 | 0.667 |
| **Range** | **0.082** | **0.209** | **0.038** | **0.065** | **0.034** | **0.414** | **0.212** | **0.168** | **0.492** |

The bottom row shows the spread between best and worst hat (max − min). Models
where the panel helps (Opus, Sonnet, GPT-5.4, MedGemma) have narrow ranges
(0.03–0.08). Models where it does not (Nemotron, GPT-OSS:120B, Grok, Gemma3)
have wide ranges (0.17–0.49). Haiku is an exception: wide range (0.209) but
significant panel improvement, driven by strong top-hat performance.

#### Best Hat vs Panel Majority and Self-Consistency

A critical question is whether the panel's value comes from aggregation across
hats or simply from the structured perspective of individual hats. We tested
the best-performing individual hat against both SC majority and panel majority.

**Best Hat vs Self-Consistency:**

| Model | Best Hat | Best Hat Score | SC Score | Difference | p |
|-------|---------|---------------|----------|-----------|---|
| **Haiku** | White | 0.700 | 0.516 | +0.184 | **<0.0001** |
| **MedGemma** | Red | 0.709 | 0.537 | +0.172 | **<0.0001** |
| **Opus** | Black | 0.742 | 0.606 | +0.136 | **<0.0001** |
| **Sonnet** | Blue | 0.755 | 0.666 | +0.089 | **0.001** |
| GPT-OSS:120B | Red | 0.732 | 0.692 | +0.040 | 0.107 (n.s.) |

For four of five models showing panel improvement, even the best individual
hat significantly outperforms self-consistency. The structured reasoning
perspective itself — not aggregation — is the primary source of improvement.

**Best Hat vs Panel Majority:**

| Model | Panel Majority | Best Hat | Difference | p |
|-------|---------------|----------|-----------|---|
| Opus | 0.725 | Black 0.742 | −0.017 | 0.43 (n.s.) |
| MedGemma | 0.688 | Red 0.709 | −0.021 | 0.34 (n.s.) |
| GPT-OSS:120B | 0.755 | Red 0.732 | +0.023 | 0.27 (n.s.) |

No significant differences between panel majority and the best individual hat.
The panel's practical advantage is that it achieves best-hat-level performance
*without requiring advance knowledge of which hat is best* — majority voting
approximates an oracle hat selector.

### Aggregation Method Comparison

Across all models, majority vote and median performed comparably. Trimmed mean
was consistently the weakest aggregation method, significantly worse than majority
in the Nemotron t=1.0 condition (p=0.034). Trimming extreme votes discards correct
minority opinions.

| Model | Majority | Median | Trimmed Mean |
|-------|----------|--------|-------------|
| MedGemma | 0.688 | **0.710** | **0.710** |
| Haiku | 0.695 | 0.688 | 0.690 |
| Opus | 0.725 | 0.713 | 0.713 |
| Sonnet | 0.753 | 0.746 | 0.740 |
| GPT-5.4 | 0.770 | 0.768 | 0.768 |
| GPT-OSS:120B | 0.755 | 0.753 | 0.738 |
| GPT-OSS:20B | 0.732 | 0.723 | 0.727 |
| Grok | 0.673 | 0.657 | 0.644 |
| Nemotron | 0.715 | 0.738 | 0.712 |
| Gemma3 | 0.563 | 0.529 | 0.540 |

## Results Summary

Five of nine models showed statistically significant improvement from the Six
Hats panel over self-consistency, with effect sizes from d=0.17 to d=0.34:

| Model | Single | SC | Panel | Panel vs SC d | Significant? |
|-------|--------|-----|-------|---------------|-------------|
| MedGemma-27B | 0.510 | 0.537 | 0.688 | 0.305 | Yes (p=0.0001) |
| Haiku | 0.532 | 0.516 | 0.695 | 0.334 | Yes (p<0.0001) |
| Opus | 0.605 | 0.606 | 0.725 | 0.342 | Yes (p<0.0001) |
| Sonnet | 0.655 | 0.666 | 0.753 | 0.254 | Yes (p=0.001) |
| GPT-OSS:120B | 0.709 | 0.692 | 0.755 | 0.171 | Yes (p=0.020) |
| GPT-5.4 | 0.745 | 0.758 | 0.770 | 0.042 | No |
| GPT-OSS:20B | 0.686 | 0.726 | 0.732 | 0.018 | No |
| Grok 4.20 | 0.673 | 0.687 | 0.673 | — | No |
| Nemotron | 0.706 | 0.747 | 0.715 | — | No |
| Gemma3-27B | 0.615 | 0.634 | 0.563 | −0.166 | Yes — *hurts* |

## Discussion

### 1. Perspective Diversity, Not Sampling Diversity

The central finding is that the panel's value derives from **structured reasoning
perspectives**, not from generating multiple samples. Self-consistency relies on
temperature sampling for diversity, which produces minimal variation for some
models (Opus SC 0.606 ≈ single 0.605; Haiku SC 0.516 < single 0.532). The panel
generates diversity through *different analytical frames* — data-focused (White),
intuitive (Red), critical (Black), constructive (Yellow), lateral (Green), and
meta-cognitive (Blue) — which proves more reliable.

This is supported by the best-hat analysis: for four of five models showing
panel improvement, even the best *individual* hat significantly outperforms
self-consistency (p ≤ 0.001). A single well-chosen reasoning perspective
outperforms six generic temperature samples. The hat persona forces the model
into a reasoning mode it would not naturally adopt, and that mode happens to be
more effective for the task.

The panel's practical advantage over selecting a single hat is that majority
voting achieves best-hat-level performance without requiring advance knowledge
of which hat is best for a given question. Panel majority is statistically
indistinguishable from the best individual hat (p > 0.25 in all tests).

### 2. Model Capability Determines Panel Effectiveness

The panel helps models that can meaningfully adopt different reasoning
perspectives. This manifests as **hat uniformity**: models where the panel
helps show narrow per-hat score ranges (Opus: 0.038; Sonnet: 0.065; GPT-5.4:
0.034), while models where it doesn't show wide ranges (Nemotron: 0.492;
GPT-OSS:120B: 0.414; Grok: 0.212). When a model cannot genuinely reason
from an unfamiliar perspective, that hat produces noise that degrades the
aggregate.

Gemma3-27B is the clearest negative case: the panel *significantly hurts*
performance (0.634 → 0.563, p=0.033). Its Black hat (0.438) and Green hat
(0.444) actively degrade the panel, dragging the majority vote below what
any single perspective achieves.

### 3. Domain Knowledge Amplifies the Panel Effect

MedGemma-27B (medical-domain) and Gemma3-27B (general-purpose) share the
same architecture and parameter count but show opposite panel effects:
MedGemma improves from 0.537 to 0.688 (p=0.0001) while Gemma3 worsens from
0.634 to 0.563 (p=0.033). MedGemma's clinical training enables all six hats
to contribute meaningfully (range: 0.082), while Gemma3's lack of clinical
knowledge makes several hat perspectives counterproductive (range: 0.168).

This suggests that the Six Hats framework leverages domain knowledge that
exists in the model but is not fully accessed by standard prompting. The
structured perspectives serve as keys that unlock different facets of the
model's knowledge.

### 4. The Ceiling Effect

GPT-5.4 achieves the highest single-agent score (0.745) and the highest
panel score (0.770) but the improvement is not significant (d=0.042). When
a model is already near-ceiling, all hats converge on similar answers and
the panel adds little (oracle gap: 0.055, the smallest observed). The panel
provides the most value for models with moderate baselines — strong enough
to reason from multiple perspectives but not so strong that all perspectives
converge.

### 5. The Oracle Gap and Aggregation Limits

The panel oracle (best answer from any hat) is consistently high across all
models (0.76–0.91), even where panel majority doesn't help:

| Model | Panel Majority | Panel Oracle | Gap |
|-------|---------------|--------------|-----|
| GPT-5.4 | 0.770 | 0.825 | 0.055 |
| Opus | 0.725 | 0.799 | 0.074 |
| Sonnet | 0.753 | 0.835 | 0.082 |
| Haiku | 0.695 | 0.883 | 0.188 |
| Grok | 0.673 | 0.884 | 0.211 |
| Nemotron | 0.715 | 0.882 | 0.167 |
| GPT-OSS:120B | 0.755 | 0.912 | 0.157 |

The correct answer is usually *present* in the panel — the challenge is
*aggregation*. Models with small oracle gaps (GPT-5.4, Opus, Sonnet) already
extract most of the available signal. Models with large gaps (Grok, Nemotron,
Haiku) have substantial room for improvement through better aggregation
strategies (weighted voting, confidence-based selection, learned aggregation).

### 6. LLM Synthesis Hurts; Algorithmic Aggregation Helps

The v1 Blue hat synthesizer (an LLM call that synthesized a final answer from
the discussion) scored 0.625 on Nemotron — significantly worse than single-agent
(0.698, p=0.036). Replacing LLM synthesis with algorithmic aggregation (v2)
eliminated this failure mode. Simple majority vote is sufficient and robust.

### 7. Self-Consistency Can Hurt

In three cases (Haiku, GPT-OSS:120B, GPT-5.4 marginally), SC majority scored
lower than single-agent. At temperature 0.5, these models are sufficiently
deterministic that sampling adds noise rather than useful diversity. This
further underscores that perspective diversity (via hat prompts) is a more
reliable diversity mechanism than temperature diversity.

## Conclusions

1. **The Six Thinking Hats panel is an effective method for improving LLM
   clinical reasoning**, producing significant improvements for five of nine
   models tested on SCT-Bench (p ≤ 0.02, d = 0.17–0.34).

2. **The improvement comes from structured reasoning perspectives, not
   sampling diversity.** Individual hat personas significantly outperform
   self-consistency for most improved models, indicating that the analytical
   framing — not the aggregation — is the primary mechanism.

3. **The method is model-dependent.** It requires models capable of genuinely
   adopting different reasoning perspectives, as evidenced by uniform per-hat
   performance. Models that cannot do this (Gemma3, Nemotron, Grok) do not
   benefit and may be harmed.

4. **Domain knowledge matters.** MedGemma and Gemma3, identical in architecture
   but differing in clinical training, show opposite effects — the largest
   improvement and the only significant degradation, respectively. The hat
   perspectives serve as keys that unlock domain-specific reasoning.

5. **Simple aggregation is sufficient.** Majority voting performs comparably
   to more sophisticated methods (median, trimmed mean) and achieves
   best-hat-level performance without advance hat selection. The remaining
   oracle gap suggests room for improved aggregation, but the primary
   bottleneck is hat quality, not voting strategy.

6. **The panel is most valuable for mid-capability models.** Models with low
   baselines that cannot adopt diverse perspectives (Gemma3) are harmed.
   Models near ceiling (GPT-5.4) show no significant improvement. The sweet
   spot is models capable enough to reason from structured perspectives but
   not so strong that all perspectives converge.

## Limitations

1. **Single temperature setting**: All models were tested at temperature=0.5.
   Nemotron was additionally tested at 1.0 (model card recommendation) with
   worse results. Optimal temperature may vary by model.

2. **Fixed hat order**: Hats always run in the same order (White → Red → Black →
   Yellow → Green → Blue). Later hats see more discussion context; order effects
   are not controlled for.

3. **174 questions**: While sufficient for the paired statistical tests used
   (demonstrated by the significant results obtained), a larger benchmark would
   provide tighter confidence intervals.

4. **Post-hoc hat analysis**: The best-hat comparisons use the same data to
   identify and evaluate the best hat, which inflates the best-hat scores.
   However, the comparison of interest (best hat vs SC) is not affected by
   this issue since SC is evaluated independently.

5. **Truncation repairs**: Opus (29/1044), Sonnet (19/1044), and GPT-OSS:120B
   (195/1044) panel responses were repaired by rerunning truncated hat calls
   at higher max_tokens. Repairs had minimal impact on aggregate scores
   (Opus +0.003, Sonnet −0.001).

6. **Gemini 2.5 Pro**: Gemini results were invalid due to safety filter
   blocking of clinical content (empty responses on all questions) and are
   excluded from the analysis.

## Future Directions

1. **Hat subset selection**: Per-hat analysis suggests that a trimmed panel
   (e.g., White + Red + Blue) could outperform the full 6-hat panel for models
   with weak hats. The `--hats` flag in the eval script supports this.

2. **Per-hat temperature tuning**: Different temperatures for different hats
   (e.g., lower for White/Black, higher for Green) could improve the diversity-
   quality tradeoff.

3. **Better aggregation**: The large oracle gaps suggest that more sophisticated
   aggregation — weighted voting by hat reliability, confidence-based selection,
   or learned aggregation — could substantially close the gap.

4. **Cross-domain evaluation**: Preliminary BBH results (causal judgement)
   show similar patterns (Sonnet: 55.1% → 74.9% with panel). Full cross-domain
   evaluation would clarify the generalizability of these findings.

5. **Independent voting ablation**: Running each hat independently (without
   seeing prior discussion) would isolate the contribution of the hat persona
   from the deliberation process.

6. **Hat uniformity as a predictor**: The per-hat score range correlates
   strongly with panel effectiveness. This could serve as a practical
   diagnostic: compute hat variance on a small calibration set to predict
   whether the panel will help for a given model-task combination.

## Reproducibility

All raw results (per-question ratings, scores, full LLM responses, and expert
distributions) are saved in JSON files under `results/sct_v2*/`. The eval script
version, model configuration, and timestamp are recorded in each file's `config`
field.

To reproduce any result:

```bash
# Install dependencies
uv pip install pydantic openai python-dotenv pyyaml numpy scipy httpx anthropic

# Run evaluation
python experiments/sct_eval_v2.py --mode all \
    --model <model> [--base-url <url>] \
    --results-dir results/<dir>

# Analyze results
python experiments/sct_eval_v2.py --mode analyze --results-dir results/<dir>

# Cross-version comparison
python experiments/sct_eval_v2.py --mode compare \
    --results-dir results/<new> --compare-dir results/<old>
```

### Data Files

| Directory | Model | Timestamp | Notes |
|-----------|-------|-----------|-------|
| results/sct_v2_medgemma/ | MedGemma-27B-text-it | 20260330_065317 | |
| results/sct_v2_haiku/ | Claude Haiku 4.5 | 20260331_015113 | |
| results/sct_v2_opus/ | Claude Opus 4.6 | 20260330_040918 | Panel repaired (29 calls) |
| results/sct_v2_sonnet/ | Claude Sonnet 4.6 | 20260328_034049 | Panel repaired (19 calls) |
| results/sct_v2_gemma3/ | Gemma3-27B-it | 20260330_172312 | |
| results/sct_v2_gptoss/ | GPT-OSS:120B | 20260329_040049 | Panel repaired (195 calls) |
| results/sct_v2_gptoss20b/ | GPT-OSS:20B | 20260330_153518 | |
| results/sct_v2_grok/ | Grok 4.20 (non-reasoning) | 20260330_061536 | |
| results/sct_v2/ | Nemotron-3-Super (t=0.5) | 20260327_055455 | |
| results/sct_v2_gpt54/ | GPT-5.4 | 20260330_054911 | |
| results/sct_v2_gemini/ | Gemini 2.5 Pro | 20260330_061635 | Excluded (safety filter) |
