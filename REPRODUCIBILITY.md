# Reproducibility Guide

This document provides a complete inventory of all evaluation scripts, input
data, models, parameters, and saved results for reproducing every quantitative
claim in the paper.

## Models

| Component | Model | Dimensions | Source |
|-----------|-------|-----------|--------|
| Embedding (all evals) | BAAI/bge-base-en-v1.5 | 768 | [HuggingFace](https://huggingface.co/BAAI/bge-base-en-v1.5) |
| Hash embedding (deterministic fallback) | Built-in hash | varies | BEAR framework |
| LLM (output/role divergence) | mistral-nemo-instruct-2407 | — | [LM Studio](https://lmstudio.ai) |
| LLM (tool scaling dispatch) | nvidia/nemotron-3-super (Q4_K_M) | — | [LM Studio](https://lmstudio.ai) |
| LLM (ecosystem LLM evals) | mistral-nemo-instruct-2407 | — | [LM Studio](https://lmstudio.ai) |
| LLM (bear_parlor Yellow hat) | nvidia/nemotron-3-super (Q4_K_M) | — | [LM Studio](https://lmstudio.ai) |
| LLM (bear_parlor White/Black) | Claude Opus/Sonnet | — | [Anthropic API](https://console.anthropic.com) |
| LLM (bear_parlor Green) | Claude Haiku | — | [Anthropic API](https://console.anthropic.com) |
| LLM (bear_parlor Red/Blue) | Gemini Flash | — | [Google AI](https://ai.google.dev) |
| Embedding (backend comparison) | nvidia/llama-embed-nemotron-8b | 4096 | [HuggingFace](https://huggingface.co/nvidia/llama-embed-nemotron-8b) |

The embedding model is automatically downloaded on first use via
`sentence-transformers`. Hash embeddings are a deterministic fallback that
produces reproducible results without model downloads.

## Statistical Methods

All evaluation scripts report 95% bootstrap confidence intervals (10,000
iterations, seed=42) on aggregate metrics. Shared statistical utilities are
in `paper/evaluation/stat_utils.py`.

| Method | Used In | Purpose |
|--------|---------|---------|
| Bootstrap CI (10k iter) | eval_retrieval, eval_ablation, eval_divergence, eval_scalability, eval_tool_scaling, eval_baseline_comparison | Confidence intervals on F1, divergence, token efficiency |
| One-sample t-test | eval_significance | Discrimination ratio > 1.0 |
| Wilcoxon signed-rank | eval_significance | Non-parametric alternative |
| Welch's t-test | eval_significance, eval5_ga, eval8_diploid, eval9_diploid, eval2_ablation | Independent group comparisons |
| Mann-Whitney U | eval_significance, eval5_ga, eval8_diploid, eval9_diploid, eval2_ablation | Non-parametric group comparisons |
| Cohen's d | eval_significance, eval5_ga, eval8_diploid, eval9_diploid, eval2_ablation | Effect sizes |
| Holm-Bonferroni | eval_significance | Multiple comparison correction (6 hats) |
| Fisher's exact test | eval_baseline_comparison | Novel context recall (BEAR vs CPA) |
| Bootstrap paired test | eval_retrieval_backends | Backend pairwise comparisons |
| Linear regression | eval_evolution, eval1_population_dynamics | Trend analysis with p-values |

## Environment

```
Python >= 3.11
Platform: macOS (Apple Silicon), Linux, or Windows (via WSL2)
```

### Installation

```bash
# Create venv and install core + eval dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[all,eval]"

# GPU acceleration (CUDA systems only)
uv pip install flash-attn --no-build-isolation   # for Nemotron-Embed-8B

# External benchmarks (ToolBench, MetaTool)
uv pip install datasets                          # HuggingFace datasets for ToolBench
```

### API Keys

| Key | Required For | Set In |
|-----|-------------|--------|
| `ANTHROPIC_API_KEY` | Paper 2 bear_parlor (White/Black/Green hats) | `.env` |
| `GEMINI_API_KEY` | Paper 2 bear_parlor (Red/Blue hats) | `.env` |
| `HF_TOKEN` | Optional: faster HuggingFace downloads | `.env` or `export` |

All Paper 0 and Paper 1 evals run without API keys (deterministic or local LLM).

### LLM Requirements

| Model | Used By | Backend |
|-------|---------|---------|
| mistral-nemo-instruct-2407 | Output/role divergence, ecosystem LLM evals | LM Studio |
| nvidia/nemotron-3-super | Output divergence (2nd model), tool dispatch | LM Studio |

### Windows / WSL2 Setup

All eval scripts auto-detect WSL and resolve the Windows host IP for
LM Studio access via `ip route show default`. Ensure LM Studio has
**"Serve on Local Network"** enabled.

```bash
# In WSL2, activate the venv and install
source .venv/bin/activate
uv pip install -e ".[all,eval]"

# Verify LM Studio is reachable
curl -s http://$(ip route show default | awk '{print $3}'):1234/v1/models
```

---

## Quick Reference: Running All Evaluations

### Paper 0: Retrieval-Governed Prompting

```bash
cd /path/to/bear

# --- Deterministic evals (no LLM, ~20 min) ---
./run_paper0_evals.sh

# --- Backend comparison with ITR + governance ablation (~30 min with GPU) ---
python paper/evaluation/eval_retrieval_backends.py --all \
  2>&1 | tee paper/evaluation/results/eval_retrieval_backends_output.txt

python paper/evaluation/eval_governance_ablation.py \
  2>&1 | tee paper/evaluation/results/eval_governance_ablation_output.txt

# --- LLM output divergence (requires LM Studio, ~20 min per model) ---
# Load mistral-nemo-instruct-2407 in LM Studio, then:
python paper/evaluation/eval_output_divergence.py --semantic --temperature 0.0 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo_sem_t0.txt
python paper/evaluation/eval_output_divergence.py --semantic --temperature 0.7 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo_sem_t07.txt
python paper/evaluation/eval_output_divergence.py --temperature 0.0 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo_hash_t0.txt
python paper/evaluation/eval_output_divergence.py --temperature 0.7 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo_hash_t07.txt

# Swap to nvidia/nemotron-3-super in LM Studio, then:
python paper/evaluation/eval_output_divergence.py --model nvidia/nemotron-3-super --semantic --temperature 0.0 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo3_sem_t0.txt
python paper/evaluation/eval_output_divergence.py --model nvidia/nemotron-3-super --semantic --temperature 0.7 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo3_sem_t07.txt
python paper/evaluation/eval_output_divergence.py --model nvidia/nemotron-3-super --temperature 0.0 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo3_hash_t0.txt
python paper/evaluation/eval_output_divergence.py --model nvidia/nemotron-3-super --temperature 0.7 \
  2>&1 | tee paper/evaluation/results/eval_output_divergence_nemo3_hash_t07.txt

# --- Role divergence (requires LM Studio with mistral-nemo) ---
python paper/evaluation/eval_role_divergence.py \
  2>&1 | tee paper/evaluation/results/eval_role_divergence_output.txt

# --- External benchmarks: retrieval-level (no LLM, GPU recommended) ---
python paper/evaluation/toolbench_setup.py                    # download data
python paper/evaluation/eval_toolbench.py --metatool-only \
  2>&1 | tee paper/evaluation/results/eval_metatool_full_output.txt
python paper/evaluation/eval_toolbench.py --toolbench-only \
  2>&1 | tee paper/evaluation/results/eval_toolbench_full_output.txt

# --- External benchmarks: end-to-end with LLM (requires LM Studio) ---
# Load nvidia/nemotron-3-super in LM Studio, then:
python paper/evaluation/eval_toolbench_e2e.py --all \
  2>&1 | tee paper/evaluation/results/eval_toolbench_e2e_output.txt

# Quick test (100 queries, default backends only):
python paper/evaluation/eval_toolbench_e2e.py --max-queries 100 \
  2>&1 | tee paper/evaluation/results/eval_toolbench_e2e_quick.txt
```

### Paper 1: Behavioral Genetics

```bash
# --- Deterministic evals (no LLM, ~60 min) ---
./run_paper1_evals.sh

# --- LLM-mediated evals (requires LM Studio with mistral-nemo) ---
python examples/evolutionary_ecosystem/eval/eval6b_llm_dialogue.py \
  2>&1 | tee paper1_eval6b.log
python examples/evolutionary_ecosystem/eval/eval7b_llm_mutation.py \
  2>&1 | tee paper1_eval7b.log
python examples/evolutionary_ecosystem/eval/eval5b_llm_breeding.py \
  2>&1 | tee paper1_eval5b.log
```

### Paper 2: Cognitive Filtering (TiiS)

```bash
# --- Deterministic evals (no LLM, analyzes existing session logs) ---
./run_paper2_evals.sh

# --- Full suite including LLM evals ---
# Requires: ANTHROPIC_API_KEY, GEMINI_API_KEY, LM Studio with mistral-nemo
./run_paper2_evals.sh --all
```

---

## Part 1: Pet Simulation & Customer Support Evaluations (Sections 11.1–11.8)

All scripts are in `paper/evaluation/`. Run from the repo root.

### Quick Start

```bash
# Run all deterministic evaluations (no LLM needed, hash embeddings)
python paper/evaluation/run_all.py

# Run with semantic embeddings (BAAI/bge-base-en-v1.5)
python paper/evaluation/eval_retrieval.py --semantic
python paper/evaluation/eval_scalability.py --semantic
python paper/evaluation/eval_ablation.py --semantic
python paper/evaluation/eval_divergence.py --semantic
python paper/evaluation/eval_evolution.py --semantic
```

### Semantic Mode Results

The following scripts support both hash (deterministic) and semantic
(BAAI/bge-base-en-v1.5) embedding modes. Semantic results validate that
findings hold with real learned embeddings, not just hash fallbacks.

| Script | Flag | Saved Semantic Results |
|--------|------|----------------------|
| `eval_retrieval.py` | `--semantic` | `results/eval_retrieval_semantic_output.txt` |
| `eval_ablation.py` | `--semantic` | `results/eval_ablation_semantic_output.txt` |
| `eval_divergence.py` | `--semantic` | `results/eval_divergence_semantic_output.txt` |
| `eval_evolution.py` | `--semantic` | `results/eval_evolution_semantic_output.txt` |

### Evaluation Inventory

| Section | Script | Input Data | Seed / Determinism | Saved Results |
|---------|--------|------------|-------------------|---------------|
| 11.1 Retrieval Quality | `eval_retrieval.py` | Pet Sim corpus (`examples/pet_sim/instructions/`) | Deterministic (hash mode) | `results/eval_retrieval_output.txt` |
| 11.2 Scalability | `eval_scalability.py` | Procedurally generated customer support corpus | Deterministic | `scalability_results.csv`, `results/eval_scalability_output.txt` |
| 11.3 CPA Comparison | `eval_baseline_comparison.py` | Procedurally generated corpus (same as 11.2) | Deterministic | `baseline_comparison_results.csv`, `results/eval_baseline_output.txt` |
| 11.4 Parameter Sensitivity | `eval_ablation.py` | Pet Sim corpus | Deterministic (hash mode) | `results/eval_ablation_output.txt` |
| 11.5 Behavioral Divergence | `eval_divergence.py` | Pet Sim corpus | Deterministic (hash mode) | `results/eval_divergence_output.txt` |
| 11.5 Output Divergence | `eval_output_divergence.py` | Pet Sim corpus | LLM-dependent (temp=0.7) | **Requires live LLM** |
| 11.6 Evolution Dynamics | `eval_evolution.py` | Pet Sim corpus | Deterministic (hash mode) | `evolution_trajectory.csv`, `evolution_breeding.csv`, `evolution_events.csv`, `evolution_profiles.csv`, `results/eval_evolution_output.txt` |
| 11.7 Breeding Dynamics | `eval_evolution.py` (breeding section) | Pet Sim corpus | Deterministic | Included in evolution CSVs |
| 11.8 Architectural Properties | N/A (structural analysis) | — | — | — |
| 11.9 Tool-Instruction Retrieval | `eval_tool_scaling.py` | Procedurally generated tool corpus (50 tools) | Deterministic | `results/eval_tool_scaling_output.txt` |
| — Tool Composition | `eval_tool_composition.py` | Programmatic corpus | Deterministic | `results/eval_tool_composition_output.txt` |
| — Refined Query | `eval_refined_query.py` | Programmatic corpus | Deterministic | `results/eval_refined_output.txt` |

### Input Data

- **Pet Simulation corpus**: `examples/pet_sim/instructions/` — 8 YAML files, 58 instructions
  - `cat_base.yaml`, `dog_base.yaml`, `interactions.yaml`, `moods.yaml`,
    `stimulus_base.yaml`, `commands.yaml`, `constraints.yaml`, `inter_pet.yaml`
- **Customer support corpus**: Procedurally generated inside each script
  - 10 departments, N agents (10–500), 4 instruction categories
  - Generation is deterministic and embedded in the script
- **Tool corpus**: Procedurally generated inside `eval_tool_scaling.py`
  - 50 tools across 8 domains, deterministic

### Notes on LLM-Dependent Evals

`eval_output_divergence.py` measures whether BEAR-composed prompts produce
measurably different LLM outputs. The paper reports an 8-condition matrix:
2 LLMs × 2 retrieval modes × 2 temperatures.

| Flag | Options | Default |
|------|---------|---------|
| `--model` | Any LM Studio model ID | `mistral-nemo-instruct-2407` |
| `--semantic` | Use BGE-base-en-v1.5 embeddings | Hash embeddings |
| `--temperature` | 0.0 (causal test) or 0.7 (practical test) | 0.0 |

28 scenario pairs per run. Metrics: content diff, cosine distance, Hausdorff
distance with bootstrap 95% CIs, paired t-test, Wilcoxon signed-rank, Cohen's d.

`eval_role_divergence.py` measures within-species and cross-species divergence
under three prompting strategies (BEAR, role, static). Requires LM Studio.

Due to LLM non-determinism (especially Mistral Nemo at T=0.0), exact values
may vary across runs. Nemotron-3-Super is perfectly deterministic at T=0.0.

### Notes on External Benchmark Evals

`eval_toolbench.py` evaluates BEAR on independently authored tool corpora:

| Benchmark | Tools | Queries | Source |
|-----------|-------|---------|--------|
| MetaTool | 199 | 20,614 + 497 multi-tool | GitHub (auto-downloaded) |
| ToolBench | 16,464 | varies by split | HuggingFace `datasets` |

Run `toolbench_setup.py` first to download data.

**Retrieval-level** (`eval_toolbench.py`): No LLM needed. Tests 11 conditions
(8 governed backends + 3 ungoverned ablations) with Recall@k, NDCG@k, bootstrap
CIs, paired t-tests, Wilcoxon signed-rank, and Cohen's d.

**End-to-end** (`eval_toolbench_e2e.py`): Requires LM Studio with
nvidia/nemotron-3-super. Tests full pipeline: retrieval → LLM tool selection →
accuracy. Measures exact API match and tool-level match with bootstrap CIs and
paired statistical tests. 1,100 queries × ~1-2s per LLM call ≈ 30 min per condition.

---

## Part 2: Evolutionary Ecosystem Evaluations (Sections 11.10–11.16)

All scripts are in `examples/evolutionary_ecosystem/eval/`. Run from the repo root.

### Quick Start

```bash
# Run individual evaluations (headless, no LLM)
python examples/evolutionary_ecosystem/eval/eval1_population_dynamics.py
python examples/evolutionary_ecosystem/eval/eval2_dual_pathway_ablation.py
python examples/evolutionary_ecosystem/eval/eval3_inheritance_fidelity.py
python examples/evolutionary_ecosystem/eval/eval3b_locus_breeding.py
python examples/evolutionary_ecosystem/eval/eval4_epoch_phenotype_shift.py
python examples/evolutionary_ecosystem/eval/eval5_ga_baseline.py
python examples/evolutionary_ecosystem/eval/eval6_dialogue_quality.py
python examples/evolutionary_ecosystem/eval/eval7_mutation_diversity.py
python examples/evolutionary_ecosystem/eval/eval8_evolution_dynamics.py
python examples/evolutionary_ecosystem/eval/eval8_diploid_diversity.py
python examples/evolutionary_ecosystem/eval/eval9_diploid_selection.py
```

### Evaluation Inventory

| Section | Script | Seed | Ticks | Trials | Saved Results |
|---------|--------|------|-------|--------|---------------|
| 11.10 Population Dynamics | `eval1_population_dynamics.py` | 42 | 60,000 | 1 | `results/eval1_results.json`, `results/eval1_dynamics.png` |
| 11.11 Dual-Pathway Ablation | `eval2_dual_pathway_ablation.py` | 42, 1042, 2042 | 30,000 | 3×2 | `results/eval2_results.json`, `results/eval2_ablation.png` |
| 11.12 Inheritance Fidelity | `eval3_inheritance_fidelity.py` | 42 | — | 10 pairs, 3 chains | `results/eval3_results.json`, `results/eval3_inheritance.png` |
| 11.12b Locus-Based Breeding | `eval3b_locus_breeding.py` | 42 | — | 8 pairs × 5 offspring | `results/eval3b_results.json`, `results/eval3b_breeding.png` |
| 11.13 Epoch Phenotype Shift | `eval4_epoch_phenotype_shift.py` | 42, 1042, 2042 | 30,000 | 5×3 | `results/eval4_results.json`, `results/eval4_epoch_shift.png` |
| 11.14 GA Baseline | `eval5_ga_baseline.py` | 42, 1042, 2042 | 30,000 | 3×2 | `results/eval5_results.json`, `results/eval5_ga_baseline.png` |
| 11.15 Retrieval Diversity | `eval6_dialogue_quality.py` | — | — | 4 creatures × 5 scenarios | `results/eval6_results.json`, `results/eval6_dialogue.png` |
| 11.16 Mutation Diversity | `eval7_mutation_diversity.py` | 42 | — | 4 pairs × 20 offspring | `results/eval7_results.json`, `results/eval7_mutation.png` |
| 11.17 Evolution Dynamics | `eval8_evolution_dynamics.py` | 42 | — | 4 creatures | `results/eval8_results.json`, `results/eval8_dynamics.png` |
| 11.18 Diploid Diversity | `eval8_diploid_diversity.py` | 42, 1042, 2042 | — | 3×3 (5 gen each) | `results/eval8_diploid_results.json`, `results/eval8_diploid_diversity.png` |
| 11.19 Diploid Selection Pressure | `eval9_diploid_selection.py` | 42, 1042, 2042 | 15,000 | 3×3 | `results/eval9_diploid_results.json`, `results/eval9_diploid_selection.png` |

### Shared Infrastructure

- **Harness**: `examples/evolutionary_ecosystem/eval/harness.py`
  - Provides: `make_world()`, `run_simulation()`, `make_creature()`, `breed_deterministic()`,
    `breed_bear_diploid()`, `make_locus_registry()`
  - Gene bank: 8 hand-crafted gene sets (Bold, Timid, Curious, Calm, Energetic, Nurturing, Cunning, Moody)
  - Simulation parameter overrides for sustainable multi-generational runs
- **Embedding model**: BAAI/bge-base-en-v1.5 (shared singleton across all evals)
- **LLM**: None — all evals run in headless mode with deterministic text splicing

### Simulation Parameters (Eval Harness Overrides)

| Parameter | Default (interactive) | Eval Override | Purpose |
|-----------|----------------------|---------------|---------|
| BASE_METABOLISM | 0.02 | 0.008 | Less energy drain |
| STARVATION_DMG | 0.8 | 0.3 | Slower starvation death |
| FOOD_SPAWN_BASE | 0.15 | 0.30 | More food |
| MAX_FOOD | 30 | 50 | More food cap |
| FOOD_ENERGY | 25 | 35 | More energy per food |
| FOOD_PICKUP_RANGE | 0.8 | 1.2 | Easier to eat |
| BREED_COOLDOWN | 60 | 30 | Faster breeding cycles |
| BREED_HAPPINESS | 65 | 55 | Easier to breed |
| BREED_DISTANCE | 2.5 | 3.5 | Wider breed range |
| MAX_AGE_MIN | 90 | 150 | Longer lifespan |
| MAX_AGE_MAX | 150 | 250 | Longer lifespan |
| MAX_POPULATION | 12 | 16 | Larger carrying capacity |
| PREDATOR_SPAWN_INTERVAL | 90 | 200 | Less frequent raids |
| PREDATOR_ATTACK_DAMAGE | 25 | 15 | Less lethal raids |
| WEATHER_DAMAGE | 0.5 | 0.25 | Less weather damage |

### Gene Bank

The 8 gene sets used in all Evolutionary Ecosystem evaluations are defined
in `harness.py:GENE_BANK`. Each gene set contains 10 categories:
`personality`, `social_style`, `movement_style`, `reaction_pattern`,
`foraging`, `predator_defense`, `climate_survival`, `territorial`,
`stealth`, `sensory`.

### Breeding Mechanism (Eval Mode)

In eval mode, breeding uses deterministic text splicing (no LLM):
1. For each gene category, split both parent texts into sentences
2. Randomly pick sentences from each parent (coin flip per sentence)
3. 15% chance per gene to swap with a random gene bank entry (mutation)
4. Phenotype extraction via embedding similarity (deterministic)

**Locus-based crossover:** The BEAR core `breed()` function supports
locus-based breeding via `BreedingConfig(locus_key="gene_category")`.
When set, instructions are grouped by their `metadata[locus_key]` value
and one parent's version is chosen per locus (50/50 coin flip), ensuring
no gene locus is lost during inheritance. Instructions without a locus
fall back to the legacy `crossover_rate` sampling. See README.md for
full configuration options.

### Eval 3b: Locus-Based Breeding Comparison

Compares three breeding mechanisms using the same parent pairs:
1. **Text-splicing** (harness default) — blends gene text per category
2. **Legacy BEAR recombination** — random per-instruction sampling at rate 0.5
3. **Locus-based BEAR recombination** — one parent per gene_category locus

| Parameter | Value |
|-----------|-------|
| Parent pairs | 8 (from diverse gene bank archetypes) |
| Offspring per pair per method | 5 (120 total) |
| Seed | 42 |
| Embedding | BAAI/bge-base-en-v1.5 |

Metrics: behavioral coverage, parent-offspring profile similarity, gene
embedding similarity to midparent, inter-offspring diversity, corpus size,
locus inheritance balance.

```bash
python examples/evolutionary_ecosystem/eval/eval3b_locus_breeding.py
```

### Eval 8: Evolution Dynamics (Teaching, Autonomous Evolution, Breeding)

Demonstrates that BEAR instruction corpora develop through three mechanisms:
1. **Teaching** — injecting new instructions, measuring coverage stability
2. **Autonomous evolution** — gap detection generates targeted instructions
3. **Breeding** — locus-based recombination produces viable offspring

| Parameter | Value |
|-----------|-------|
| Creatures | 4 (Bold, Timid, Curious, Calm) |
| Teaching cycles | 5 (2 instructions per cycle) |
| Autonomous evolution cycles | 5 |
| Breeding pairs | 4 |
| Seed | 42 |
| Embedding | BAAI/bge-base-en-v1.5 |
| LLM | None (template-based evolution) |

```bash
python examples/evolutionary_ecosystem/eval/eval8_evolution_dynamics.py
```

### Eval 8 Diploid: Diploid vs Haploid Diversity Preservation

Compares diploid and haploid breeding across multiple generations to test
whether diploid inheritance (carrying both parents' alleles) preserves
higher genetic diversity than haploid inheritance (selecting one allele
per locus).

Three conditions:
1. **Haploid** — one parent's allele per locus
2. **Diploid-dominant** — both alleles carried; parent A's expressed
3. **Diploid-codominant** — both alleles carried and expressed together

| Parameter | Value |
|-----------|-------|
| Founders | 8 (from gene bank) |
| Generations | 5 |
| Offspring per generation | 10 |
| Trials per condition | 3 |
| Seeds | 42, 1042, 2042 |
| Embedding | BAAI/bge-base-en-v1.5 |
| Allele retention threshold | cosine similarity > 0.8 |

Metrics: genotype diversity (mean pairwise cosine distance of corpus embeddings),
phenotype diversity (behavior profiles), allele retention, heterozygosity rate,
inheritance fidelity (cosine similarity to midparent).

```bash
python examples/evolutionary_ecosystem/eval/eval8_diploid_diversity.py
```

**Key results** (from `eval8_diploid_results.json`):
- Diploid-dominant maintains ~100% allele retention vs haploid's ~22%
- Heterozygosity confirms diploid tracking: dominant ≈ 0.58, codominant ≈ 0.53, haploid = 0.0
- Statistical significance tests (t-test) included in output

### Eval 9: Diploid vs Haploid Under Selection Pressure

Uses the full Evolutionary Ecosystem simulation (evoeco) with natural and
sexual selection to test whether diploid inheritance preserves genetic
diversity better than haploid under directional selection pressure.

**Hypothesis:** Diploid inheritance preserves latent diversity via recessive
alleles that survive silently in heterozygotes, re-emerging when
environmental shifts make them advantageous.

Three conditions:
1. **Haploid (text-splicing)** — one allele per locus, deterministic text splicing
2. **Diploid-dominant (BEAR)** — both alleles carried; allele A expressed via `express()`
3. **Diploid-codominant (BEAR)** — both alleles carried and both expressed

| Parameter | Value |
|-----------|-------|
| Starting creatures | 12 |
| Max population | 30 |
| Ticks per trial | 15,000 |
| Trials per condition | 3 |
| Seeds | 42, 1042, 2042 |
| Epochs | 5 cycling (abundance → ice_age → predator_bloom → expansion → famine) |
| Embedding | BAAI/bge-base-en-v1.5 |
| Allele retention threshold | cosine similarity > 0.8 |

Selection pressures provided by the simulation:
- **Natural selection**: predators, starvation, weather damage
- **Sexual selection**: breeding_prob = avg_breeding_drive × avg_attractiveness
- **Epoch cycling**: shifting environmental pressures across 5 distinct epochs

Metrics: gene diversity (phenotype), corpus diversity (genotype),
heterozygosity, allele retention, population dynamics, births/deaths,
max generation, extinction events.

Statistical tests: Welch's t-test, Mann-Whitney U, Shapiro-Wilk normality
check (requires `scipy`).

```bash
pip install scipy  # required for statistical tests
python examples/evolutionary_ecosystem/eval/eval9_diploid_selection.py
```

**Key results** (from `eval9_diploid_results.json`):

| Metric | Haploid | Diploid-Dominant | Diploid-Codominant |
|--------|---------|------------------|-------------------|
| Gene diversity (phenotype) | 0.178 ± 0.013 | 0.184 ± 0.005 | 0.170 ± 0.008 |
| Corpus diversity (genotype) | 0.195 ± 0.005 | 0.001 ± 0.001 | 0.001 ± 0.000 |
| Heterozygosity | 0.000 ± 0.000 | 0.417 ± 0.299 | 0.250 ± 0.250 |
| Allele retention | 0.216 ± 0.073 | 0.972 ± 0.039 | 0.917 ± 0.000 |
| Avg population | 16.1 ± 0.2 | 16.1 ± 0.0 | 16.2 ± 0.1 |
| Total births | 57.0 ± 0.8 | 56.3 ± 2.1 | 56.0 ± 1.0 |
| Max generation | 18.3 ± 2.5 | 14.7 ± 0.9 | 18.0 ± 1.0 |

**Interpretation:**
- All conditions produce comparable population dynamics (~16 avg pop, ~56 births)
- Diploid conditions preserve founding alleles dramatically better (92–97% vs 22%)
- Non-zero heterozygosity confirms diploid mechanism is working (haploid = 0.0 as expected)
- Low corpus diversity for diploid conditions reflects that BEAR breeding preserves
  parent alleles faithfully rather than generating novel text, while haploid text-splicing
  produces more textually diverse (but allele-losing) offspring

**Note on trial counts:** The codominant condition completed 2 of 3 planned trials
(seeds 42, 1042). The third trial (seed 2042) timed out during metrics computation.
Results are reported with n=2 for codominant and n=3 for haploid/dominant. The
completed trials show consistent patterns across seeds.

---

## Part 2b: LLM-Mediated Evaluations (Sections 11.14b, 11.15b, 11.16b)

These evaluations extend the headless Part 2 evals by adding live LLM calls
to validate claims about LLM-mediated behavior. They require a running LLM
backend and produce results that vary by model/run, but the directional claims
(BEAR > static, LLM > deterministic) should hold across models.

All scripts are in `examples/evolutionary_ecosystem/eval/`. Run from the repo root.

### Prerequisites

```bash
# Option A: LM Studio (recommended, local)
# Start LM Studio with mistral-nemo-instruct-2407 loaded
# Server should be running at http://127.0.0.1:1234/v1
# (WSL2 users: enable "Serve on Local Network" in LM Studio settings)

# Option B: Anthropic API
export ANTHROPIC_API_KEY=sk-...
```

### Quick Start

```bash
# Eval 6b: Genome-conditioned LLM output diversity (~5 min)
python examples/evolutionary_ecosystem/eval/eval6b_llm_dialogue.py

# Eval 7b: LLM-mediated mutation diversity (~15-20 min)
python examples/evolutionary_ecosystem/eval/eval7b_llm_mutation.py

# Eval 5b: LLM-mediated breeding in simulation (~10-30 min)
python examples/evolutionary_ecosystem/eval/eval5b_llm_breeding.py
```

### CLI Arguments (all evals)

| Argument | Default | Description |
|----------|---------|-------------|
| `--model` | `mlx-community/mistral-nemo-instruct-2407:3` | LLM model ID |
| `--backend` | `auto` | `auto`, `local`, or `anthropic` |
| `--base-url` | `http://127.0.0.1:1234/v1` | Local LLM server URL |

eval5b additionally supports:
| `--ticks` | `10000` | Number of simulation ticks |
| `--seed` | `42` | Random seed |

### Evaluation Inventory

| Section | Script | LLM Calls | Temperatures | Saved Results |
|---------|--------|-----------|--------------|---------------|
| 11.15b LLM Output Diversity | `eval6b_llm_dialogue.py` | ~160 | 0.0, 0.7 | `results/eval6b_results.json`, `results/eval6b_llm_dialogue.png` |
| 11.16b LLM Mutation Diversity | `eval7b_llm_mutation.py` | ~1480 | 0.0, 0.7 | `results/eval7b_results.json`, `results/eval7b_llm_mutation.png` |
| 11.14b LLM Breeding Simulation | `eval5b_llm_breeding.py` | ~8/breed event | — | `results/eval5b_results.json`, `results/eval5b_llm_breeding.png` |

### Temperature Conditions

Each LLM eval (6b, 7b) runs at two temperatures:
- **temp=0.0**: Maximally deterministic. All output divergence is attributable
  to genome-conditioned retrieval, not LLM sampling noise. Provides the
  cleanest causal signal.
- **temp=0.7**: Realistic creative temperature. Shows the effect holds under
  natural operating conditions with more varied LLM outputs.

### What Each Eval Validates

**eval6b (Priority 1)**: The paper claims "richly differentiated slow-path LLM
conditioning." This eval actually runs an LLM with BEAR-retrieved guidance as
system prompt context and measures whether different creature genomes produce
measurably different LLM responses. Compares against a static baseline (same
prompt for all creatures). Metrics: content diff ratio, semantic cosine
distance, sentence-level Hausdorff distance.

**eval7b (Priority 2)**: The paper claims "generative advantage" of semantic
mutation. This eval adds an LLM-mediated breeding condition (using
`blend_gene()` + `mutate_gene()` from gene_engine.py) alongside the existing
deterministic and numeric GA conditions. Uses dual diversity metrics:
  - **Gene embedding diversity** (768-dim): Mean pairwise cosine distance and
    sentence-level Hausdorff distance on gene text embeddings. This is the
    primary metric for comparing text-based methods, as it captures actual
    semantic differences in generated gene descriptions.
  - **Behavior profile diversity** (7-dim): Retrieval-score-based profiles.
    These saturate near 1.0 for well-formed gene text due to normalization
    ceiling effects, so numeric GA naturally dominates this metric.
  - **Per-category diversity**: Gene embedding distance broken down by the 8
    gene categories (personality, social_style, etc.).

**eval5b (Priority 3)**: The paper describes "LLM-mediated text blending" in
breeding. This eval runs a full simulation with live LLM breeding calls and
compares population dynamics against deterministic breeding.

### Reproducibility Notes

- Results JSON files record: model name, backend, base_url, temperature,
  timestamp, and platform for each run.
- Due to LLM non-determinism, exact values vary across runs and models.
  The directional findings (BEAR-conditioned > static baseline) should be
  consistent.
- The paper results were generated with `mlx-community/mistral-nemo-instruct-2407:3`
  via LM Studio on macOS (Apple M3 Max).
- All headless baselines (eval5, eval6, eval7) remain unchanged and
  fully deterministic.

---

## Part 2c: Brainstorming Panel Evaluations (TiiS Paper)

All scripts are in `paper/evaluation/`. Session logs are in
`examples/bear_parlor/session_logs/`.

### Quick Start

```bash
# Run all brainstorming evaluations (~5-10 min, requires BAAI/bge-base-en-v1.5)
python paper/evaluation/eval_interhat_differentiation.py
python paper/evaluation/eval_role_adherence.py
python paper/evaluation/eval_significance.py
python paper/evaluation/eval_embed_only_baseline.py
python paper/evaluation/eval_dmin_sensitivity.py
python paper/evaluation/eval_temporal_evolution.py

# Or use the runner script:
./run_paper2_evals.sh
```

### Evaluation Inventory

| Analysis | Script | Input Data | Dependencies | Saved Results |
|----------|--------|------------|-------------|---------------|
| Inter-hat differentiation | `eval_interhat_differentiation.py` | 12 session logs | BAAI/bge-base-en-v1.5 | `results/interhat_differentiation.csv` |
| Role adherence | `eval_role_adherence.py` | 12 session logs + hat YAML | BAAI/bge-base-en-v1.5 | `results/role_adherence.json`, `results/role_adherence.png` |
| Statistical significance | `eval_significance.py` | 12 session logs + hat YAML | BAAI/bge-base-en-v1.5, scipy | `results/significance.json` |
| Embed-only baseline | `eval_embed_only_baseline.py` | 5 naive session logs + existing CSV | BAAI/bge-base-en-v1.5 | `results/embed_only_baseline.json`, `results/embed_only_baseline.csv` |
| d_min sensitivity | `eval_dmin_sensitivity.py` | 5 naive session logs | BAAI/bge-base-en-v1.5 | `results/dmin_sensitivity.json`, `results/dmin_sensitivity.csv`, `results/dmin_sensitivity.pdf` |
| Temporal evolution | `eval_temporal_evolution.py` | 12 session logs | BAAI/bge-base-en-v1.5 | `results/temporal_evolution.json`, `results/temporal_evolution.csv`, `results/temporal_evolution.pdf` |
| Response divergence | `eval_interhat_differentiation.py` | 12 session logs | BAAI/bge-base-en-v1.5 | `results/response_divergence.csv` |

### Session Log Files

Full session index is in `paper/evaluation/EXPERIMENT_LOG.md`. Summary below.

#### Primary sessions (heterogeneous models, v1 prompt)

| Filename | Topic | Condition | Models |
|----------|-------|-----------|--------|
| `brainstorming-hats_20260308_131430.md` | DMG | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260308_132051.md` | Stroke | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260308_132705.md` | MS | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260308_133940.md` | DMG | Naive | Heterogeneous |
| `brainstorming-hats_20260308_134610.md` | Stroke | Naive | Heterogeneous |
| `brainstorming-hats_20260308_135226.md` | MS | Naive | Heterogeneous |

#### Constant-model controls (v1 prompt)

| Filename | Topic | Condition | Models |
|----------|-------|-----------|--------|
| `brainstorming-hats_20260313_003943.md` | DMG | BEAR-guided | Uniform local 12B (Mistral Nemo) |
| `brainstorming-hats_20260313_010034.md` | DMG | Naive | Uniform local 12B (Mistral Nemo) |
| `brainstorming-hats_20260313_013537.md` | DMG | BEAR-guided | Uniform Sonnet 4.6 |
| `brainstorming-hats_20260313_014207.md` | DMG | Naive | Uniform Sonnet 4.6 |

#### Primary sessions (heterogeneous models, v4 prompt — used in paper)

| Filename | Topic | Condition | Models |
|----------|-------|-----------|--------|
| `brainstorming-hats_20260313_081554.md` | DMG | BEAR-guided | Uniform Sonnet 4.6 (v4 control) |
| `brainstorming-hats_20260313_082240.md` | DMG | Naive | Uniform Sonnet 4.6 (v4 control) |
| `brainstorming-hats_20260313_084633.md` | DMG | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260313_085257.md` | Stroke | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260313_085916.md` | MS | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260314_164032.md` | Alzheimers | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260314_164701.md` | Epilepsy | BEAR-guided | Heterogeneous |
| `brainstorming-hats_20260314_165328.md` | Alzheimers | Naive | Heterogeneous |
| `brainstorming-hats_20260314_170001.md` | Epilepsy | Naive | Heterogeneous |

#### Prompt ablation sessions (not used in paper)

| Filename | Topic | Prompt | Notes |
|----------|-------|--------|-------|
| `brainstorming-hats_20260310_214402.md` | DMG | v1 variant | Early experiment |
| `brainstorming-hats_20260311_205457.md` | DMG | v1 variant | Early experiment |
| `brainstorming-hats_20260312_224356.md` | DMG | v1 variant | Early experiment |
| `brainstorming-hats_20260313_071341.md` | DMG | v2 | Uniform Sonnet |
| `brainstorming-hats_20260313_072012.md` | DMG | v2 | Uniform Sonnet (Naive) |
| `brainstorming-hats_20260313_073201.md` | DMG | v3 | INCOMPLETE — stalled at turn 21 |

### Input Data

- **Hat instruction corpus**: `examples/bear_parlor/instructions/hats/` — 6 YAML files
  (white_hat.yaml, red_hat.yaml, black_hat.yaml, yellow_hat.yaml, green_hat.yaml, blue_hat.yaml)
- **Session logs**: Markdown files recording all turns, BEAR retrieval, diffusion events,
  and Knowledge RAG activity
- **Embedding model**: BAAI/bge-base-en-v1.5 (768-dim, same as all other evaluations)

### Key Parameters

| Parameter | Value | Used In |
|-----------|-------|---------|
| Dedup threshold ($d_{\min}$) | 0.35 | Diffusion, embed-only baseline |
| Batch size ($B$) | 6 | Cross-hat diffusion |
| Hat response temperature | 0.85 | Hat LLM generation (parlor.py) |
| Diffusion filter temperature | 0.3 | Cognitive reframing LLM call |
| Knowledge ingestion temperature | 0.3 | PDF hat-scoped directive extraction |
| Relationship scoring temperature | 0.2 | JSON extraction (structured output) |
| Corpus expansion temperature | 0.7 | New instruction generation |
| Default LLM temperature | 0.7 | Framework default (bear/llm.py) |
| Min response words | 10 | Role adherence, significance |
| Bootstrap samples | 10,000 | Significance testing |
| Bootstrap seed | 42 | Significance testing |

### Statistical Tests (eval_significance.py)

Tests whether per-response discrimination ratios are significantly > 1.0:
- **One-sample t-test** (parametric, one-sided)
- **Wilcoxon signed-rank test** (non-parametric)
- **Bootstrap 95% CI** (10,000 resamples, seed=42)
- **Cohen's d** effect size
- **Welch's t-test** and **Mann-Whitney U** for BEAR vs. Naive comparison

Requires `scipy` (`pip install scipy`).

### Embed-Only Baseline (eval_embed_only_baseline.py)

Simulates a middle-ground diffusion condition by replaying naive session logs
with cosine dedup applied (same $d_{\min} = 0.35$) but no LLM reframing.
This isolates the contribution of cognitive reframing vs. deduplication alone.

### LLM Backends Used in Sessions

| Hat | Backend | Model |
|-----|---------|-------|
| White | Anthropic | claude-opus-4-6 |
| Red | Google | gemini-3.1-flash-lite-preview |
| Black | Anthropic | claude-sonnet-4-6 |
| Yellow | LM Studio | nvidia/nemotron-3-super (Q4_K_M) |
| Green | Anthropic | claude-haiku-4-5 |
| Blue | Google | gemini-3.1-flash-image-preview |

---

## Part 3: Input Corpora

All behavioral instruction corpora used in the paper are stored in the
repository and are human-readable YAML files.

| Corpus | Location | Size | Used By |
|--------|----------|------|---------|
| Pet Simulation | `examples/pet_sim/instructions/` | 58 instructions, 8 files | Sections 11.1–11.7 |
| Customer Support | Procedurally generated | 28–675 instructions | Sections 11.2–11.3 |
| Tool Corpus | Procedurally generated | 50 tools, 8 domains | Section 11.9 |
| Evolutionary Ecosystem | Gene bank in `eval/harness.py` | 8 gene sets × 10 categories | Sections 11.10–11.19 |

---

## Reproducing All Results

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Run Pet Simulation / Customer Support evals — hash mode (~2 min)
python paper/evaluation/run_all.py

# 3. Run Pet Sim / Customer Support evals — semantic mode (~5 min)
python paper/evaluation/eval_retrieval.py --semantic > paper/evaluation/results/eval_retrieval_semantic_output.txt 2>&1
python paper/evaluation/eval_ablation.py --semantic > paper/evaluation/results/eval_ablation_semantic_output.txt 2>&1
python paper/evaluation/eval_divergence.py --semantic > paper/evaluation/results/eval_divergence_semantic_output.txt 2>&1
python paper/evaluation/eval_evolution.py --semantic > paper/evaluation/results/eval_evolution_semantic_output.txt 2>&1

# 4. Run Evolutionary Ecosystem evals (~45 min total)
python examples/evolutionary_ecosystem/eval/eval1_population_dynamics.py
python examples/evolutionary_ecosystem/eval/eval2_dual_pathway_ablation.py
python examples/evolutionary_ecosystem/eval/eval3_inheritance_fidelity.py
python examples/evolutionary_ecosystem/eval/eval3b_locus_breeding.py
python examples/evolutionary_ecosystem/eval/eval4_epoch_phenotype_shift.py
python examples/evolutionary_ecosystem/eval/eval5_ga_baseline.py
python examples/evolutionary_ecosystem/eval/eval6_dialogue_quality.py
python examples/evolutionary_ecosystem/eval/eval7_mutation_diversity.py
python examples/evolutionary_ecosystem/eval/eval8_evolution_dynamics.py
python examples/evolutionary_ecosystem/eval/eval8_diploid_diversity.py

# 4b. Run diploid selection pressure eval (~30 min, requires scipy)
pip install scipy
python examples/evolutionary_ecosystem/eval/eval9_diploid_selection.py

# 5. Run brainstorming panel evals (~5-10 min, requires BAAI/bge-base-en-v1.5)
pip install scipy  # required for eval_significance.py
python paper/evaluation/eval_interhat_differentiation.py
python paper/evaluation/eval_role_adherence.py
python paper/evaluation/eval_significance.py
python paper/evaluation/eval_embed_only_baseline.py

# 6. (Optional) Run output divergence eval (requires LM Studio + Nemotron-3-Super)
python paper/evaluation/eval_output_divergence.py --model nvidia/nemotron-3-super

# 7. (Optional) Run LLM-mediated evals (requires LM Studio or Anthropic API)
python examples/evolutionary_ecosystem/eval/eval6b_llm_dialogue.py
python examples/evolutionary_ecosystem/eval/eval7b_llm_mutation.py
python examples/evolutionary_ecosystem/eval/eval5b_llm_breeding.py
```

All results (JSON, CSV, PNG, TXT) are version-controlled in the repository
for comparison against fresh runs.

---

## Result File Inventory

### Paper Evaluation Results (`paper/evaluation/results/`)

| File | Eval | Format |
|------|------|--------|
| `eval_retrieval_output.txt` | Retrieval quality (hash) | Text |
| `eval_retrieval_semantic_output.txt` | Retrieval quality (semantic) | Text |
| `eval_scalability_output.txt` | Scalability | Text |
| `scalability_results.csv` | Scalability (raw data) | CSV |
| `eval_baseline_output.txt` | CPA baseline | Text |
| `baseline_comparison_results.csv` | CPA baseline (raw data) | CSV |
| `eval_ablation_output.txt` | Parameter sensitivity (hash) | Text |
| `eval_ablation_semantic_output.txt` | Parameter sensitivity (semantic) | Text |
| `eval_divergence_output.txt` | Behavioral divergence (hash) | Text |
| `eval_divergence_semantic_output.txt` | Behavioral divergence (semantic) | Text |
| `eval_evolution_output.txt` | Evolution dynamics (hash) | Text |
| `eval_evolution_semantic_output.txt` | Evolution dynamics (semantic) | Text |
| `evolution_trajectory.csv` | Evolution trajectory data | CSV |
| `evolution_breeding.csv` | Breeding data | CSV |
| `evolution_events.csv` | Evolution events | CSV |
| `evolution_profiles.csv` | Behavior profiles | CSV |
| `eval_tool_scaling_output.txt` | Tool corpus scaling | Text |
| `eval_tool_composition_output.txt` | Tool composition | Text |
| `eval_refined_output.txt` | Refined query | Text |
| `interhat_differentiation.csv` | Inter-hat differentiation | CSV |
| `role_adherence.json` | Role adherence | JSON |
| `role_adherence.png` | Role adherence chart | PNG |
| `significance.json` | Statistical significance | JSON |
| `embed_only_baseline.json` | Embed-only baseline | JSON |
| `embed_only_baseline.csv` | Embed-only baseline (raw) | CSV |
| `backend_comparison.json` | Retrieval backend comparison | JSON |
| `dmin_sensitivity.json` | Dedup threshold sensitivity | JSON |
| `dmin_sensitivity.csv` | Dedup threshold sensitivity (raw) | CSV |
| `dmin_sensitivity.png` | Dedup threshold chart | PNG |
| `dmin_sensitivity.pdf` | Dedup threshold chart (vector) | PDF |
| `governance_ablation.json` | Governance mechanism ablation | JSON |
| `response_divergence.csv` | Response-level divergence | CSV |
| `temporal_evolution.json` | Temporal behavioral evolution | JSON |
| `temporal_evolution.csv` | Temporal evolution (raw) | CSV |
| `temporal_evolution.png` | Temporal evolution chart | PNG |
| `temporal_evolution.pdf` | Temporal evolution chart (vector) | PDF |

### Evolutionary Ecosystem Results (`examples/evolutionary_ecosystem/eval/results/`)

| File | Eval | Format |
|------|------|--------|
| `eval1_results.json` | Population dynamics | JSON |
| `eval1_dynamics.png` | Population dynamics chart | PNG |
| `eval2_results.json` | Dual-pathway ablation | JSON |
| `eval2_ablation.png` | Ablation chart | PNG |
| `eval3_results.json` | Inheritance fidelity | JSON |
| `eval3_inheritance.png` | Inheritance chart | PNG |
| `eval3b_results.json` | Locus-based breeding | JSON |
| `eval3b_breeding.png` | Breeding comparison chart | PNG |
| `eval4_results.json` | Epoch phenotype shift | JSON |
| `eval4_epoch_shift.png` | Epoch shift chart | PNG |
| `eval5_results.json` | GA baseline | JSON |
| `eval5_ga_baseline.png` | GA baseline chart | PNG |
| `eval5b_results.json` | LLM breeding simulation | JSON |
| `eval5b_llm_breeding.png` | LLM breeding chart | PNG |
| `eval6_results.json` | Retrieval diversity | JSON |
| `eval6_dialogue.png` | Dialogue diversity chart | PNG |
| `eval6b_results.json` | LLM dialogue diversity | JSON |
| `eval6b_llm_dialogue.png` | LLM dialogue chart | PNG |
| `eval7_results.json` | Mutation diversity | JSON |
| `eval7_mutation.png` | Mutation chart | PNG |
| `eval7b_results.json` | LLM mutation diversity | JSON |
| `eval7b_llm_mutation.png` | LLM mutation chart | PNG |
| `eval8_results.json` | Evolution dynamics | JSON |
| `eval8_dynamics.png` | Evolution dynamics chart | PNG |
| `eval8_diploid_results.json` | Diploid diversity | JSON |
| `eval8_diploid_diversity.png` | Diploid diversity chart | PNG |
| `eval9_diploid_results.json` | Diploid selection pressure | JSON |

### Brainstorming Panel Results (`results/`)

52 JSON files covering panel, single-agent, consistency, and MedQA evaluations
across multiple LLM backends (Claude Haiku/Sonnet, Mixtral, Gemma, Mistral Nemo, GPT OSS).

---

## Part 4: BRAINTEASER Lateral Thinking Evaluation

Evaluates whether structured Six Thinking Hats deliberation improves LLM
performance on lateral-thinking puzzles compared to single-agent and
self-consistency baselines.

### Dataset

BRAINTEASER (Jiang et al., EMNLP 2023) — 169 sentence puzzles (SP) and
132 word puzzles (WP). Downloaded from the official repository
(`https://github.com/1171-jpg/BrainTeaser`).

```bash
# Download and convert puzzles (requires pyzipper, numpy)
pip install pyzipper numpy
python experiments/download_brainteaser.py
```

This creates:
- `experiments/brainteaser_puzzles.json` — 169 original sentence puzzles
- `experiments/brainteaser_wp_puzzles.json` — 132 original word puzzles

The ZIP is password-protected (password: `brainteaser`). On Windows without
7z, install `pyzipper` (`pip install pyzipper`) — the download script
handles AES-encrypted extraction automatically.

### Evaluation Modes

| Mode | Description | LLM Calls per Puzzle |
|------|-------------|---------------------|
| `single` | Single-agent baseline (temperature 0.0) | 1 |
| `consistency` | Self-consistency majority vote (N samples, temperature 0.5) | N (default: 4) |
| `role-majority` | Role-prompted hats, independent answers, majority vote | 4 |
| `panel` | Full Six Hats deliberation with discussion history | 4 (sequential, with context) |
| `all` | Run all conditions and compare | ~13 |
| `analyze` | Analyze saved results (no LLM calls) | 0 |

### Panel Structure

Four hats deliberate in order: Green (lateral thinking) → White (facts) →
Yellow (elegance) → Blue (synthesis). Black and Red were dropped after
analysis showed Black's critical challenges reduced accuracy across all
tested models.

Each hat sees previous hats' responses. Blue synthesizes a final answer.
The panel answer is Blue's selected choice; majority vote across all hats
is reported as an alternative aggregation.

### Running Evaluations

```bash
cd /path/to/bear

# Run all conditions on 169 SP puzzles with Claude Haiku
python experiments/brainteaser_eval.py --mode all --n 169 \
  --model claude-haiku-4-5-20251001 --puzzle-type sp

# Run with a local model via LM Studio
python experiments/brainteaser_eval.py --mode all --n 169 \
  --model nvidia/nemotron-3-super \
  --base-url http://127.0.0.1:1234/v1 --puzzle-type sp

# Run with custom hat temperatures
python experiments/brainteaser_eval.py --mode panel --n 169 \
  --model nvidia/nemotron-3-super \
  --base-url http://127.0.0.1:1234/v1 \
  -t 0.5 --blue 0.2

# Analyze all saved results
python experiments/brainteaser_eval.py --mode analyze --results-dir results
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `single` | Evaluation mode (see table above) |
| `--n` | `10` | Number of puzzles |
| `--offset` | `0` | Starting puzzle index |
| `--model` | `claude-haiku-4-5-20251001` | LLM model ID |
| `--base-url` | (none) | OpenAI-compatible server URL for local models |
| `--puzzle-type` | `both` | `sp` (sentence), `wp` (word), or `both` |
| `-t` / `--temperature` | `0.5` | Default temperature for hats / SC samples |
| `--blue` | `0.2` | Blue hat synthesis temperature |
| `--green/white/yellow/black/red` | (use `-t`) | Per-hat temperature overrides |
| `--rounds` | `1` | Discussion rounds in panel mode |
| `--thinking` | false | Enable extended thinking for reasoning models |
| `--thinking-tokens` | `2048` | Max tokens budget for thinking block |
| `--no-system-role` | false | Fold system prompts into user messages |
| `--results-dir` | `results` | Output directory for JSON result files |

### Answer Extraction

Answers are extracted from LLM responses using a regex cascade
(`extract_answer()` in `brainteaser_eval.py`):

1. High-confidence patterns: "final answer X", standalone bold letter,
   "X is the correct answer", "answer is: X"
2. Fallback: last standalone letter A–D in the response

A validation script (`experiments/llm_rejudge_brainteaser.py`) re-judges
all panel responses using Claude Haiku as an LLM judge and compares with
regex extraction. Results show the regex and LLM judge agree on Blue
synthesis accuracy to within ±1 puzzle across all tested models. Per-hat
extraction disagrees in 2–12% of responses, predominantly on Black Hat
(critical analysis mentions multiple letters) and Green Hat (creative
language). These per-hat disagreements mostly cancel out in majority vote.

```bash
# Validate regex extraction against LLM judgement
pip install anthropic python-dotenv
PYTHONIOENCODING=utf-8 python experiments/llm_rejudge_brainteaser.py
```

### Saved Results

Results are saved as `{mode}_{model}_{timestamp}.json` in the results
directory. Each file contains:
- `config`: model, temperature, hat_temps, n, offset, puzzle_type, timestamp
- `results`: per-puzzle outcomes with full response text

### Models Tested

| Model | Backend | Blue Accuracy (169 SP) |
|-------|---------|----------------------|
| Claude Haiku 4.5 | Anthropic API | 89.9% |
| Gemma 3 27B (Q4) | LM Studio | 79.9% |
| GPT-OSS-20B | LM Studio | 75.1–78.7% |
| GPT-OSS-20B (think) | LM Studio | 73.4% |
| Mixtral 8x7B (Q4) | LM Studio | 59.2–59.8% |
| Mistral Nemo 12B | LM Studio | 56.8% |

### Comparison with Self-Consistency

McNemar's exact test (paired binary outcomes) compares panel vs
self-consistency on the same puzzles:

| Model | Panel Blue | Self-Consist | McNemar p |
|-------|-----------|-------------|-----------|
| Claude Haiku (n=50) | 86.0% | 90.0% | 0.75 (n.s.) |
| GPT-OSS-20B | 78.1% | 66.9% | 0.004 * |
| GPT-OSS-20B (think) | 73.4% | 64.5% | 0.024 * |
| Gemma 27B | 79.9% | 81.7% | 0.76 (n.s.) |
| Mistral Nemo | 56.8% | 67.5% | 0.022 * (SC wins) |

Panel deliberation significantly helps GPT-OSS-20B but significantly
hurts Mistral Nemo. The effect is model-dependent.
