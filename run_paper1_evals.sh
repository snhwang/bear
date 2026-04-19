#!/usr/bin/env bash
# =============================================================================
# run_paper1_evals.sh — Behavioral Genetics (bear_air_short.tex)
#
# Paper 1 thesis: natural-language genomes with inheritance + evolution.
# Evaluates corpus evolution, breeding fidelity, population dynamics,
# diploid genetics, epoch adaptation, and GA baseline comparison.
#
# LLM REQUIREMENTS:
#   - Part A (paper/evaluation): all deterministic, no LLM needed
#   - Part B (ecosystem evals): all deterministic (headless simulation), no LLM
#   - Part C (LLM-mediated evals) requires:
#       ANTHROPIC_API_KEY for Claude Haiku (recommended)
#       OR LM Studio with a compatible model at http://127.0.0.1:1234/v1
#
# EMBEDDING MODELS (downloaded automatically):
#   - BAAI/bge-base-en-v1.5 (768-dim) — used by all ecosystem evals
#
# Usage:
#   ./run_paper1_evals.sh                              # deterministic only (~60 min)
#   ./run_paper1_evals.sh --all                                         # Anthropic auto-detect
#   ./run_paper1_evals.sh --all --backend anthropic                     # force Anthropic/Haiku
#   ./run_paper1_evals.sh --all --base-url http://localhost:11434/v1 --model gemma3:27b  # Ollama
#   ./run_paper1_evals.sh --all --base-url http://localhost:1234/v1     # LM Studio
# =============================================================================

set -e
cd "$(dirname "$0")"

# Detect WSL and resolve Windows host IP for LM Studio / Ollama
if grep -qi microsoft /proc/version 2>/dev/null; then
    WSL_HOST=$(ip route show default 2>/dev/null | awk '/default/{print $3}')
    if [[ -n "$WSL_HOST" ]]; then
        export LM_STUDIO_URL="http://${WSL_HOST}:1234/v1"
    fi
fi

ALL=false
MODEL=""
BACKEND=""
BASE_URL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all) ALL=true; shift ;;
        --model) MODEL="$2"; shift 2 ;;
        --backend) BACKEND="$2"; shift 2 ;;
        --base-url) BASE_URL="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

MODEL_ARGS=""
if [[ -n "$MODEL" ]]; then
    MODEL_ARGS="--model $MODEL"
fi
if [[ -n "$BASE_URL" ]]; then
    MODEL_ARGS="$MODEL_ARGS --base-url $BASE_URL"
elif [[ -n "$BACKEND" ]]; then
    MODEL_ARGS="$MODEL_ARGS --backend $BACKEND"
elif [[ -n "$ANTHROPIC_API_KEY" ]]; then
    MODEL_ARGS="$MODEL_ARGS --backend anthropic"
fi

EVAL_DIR="paper/evaluation"
ECO_DIR="examples/evolutionary_ecosystem/eval"
RESULTS_DIR="$EVAL_DIR/results"
mkdir -p "$RESULTS_DIR"

echo "========================================"
echo "  Paper 1: Behavioral Genetics"
echo "========================================"
echo ""

# =====================================================================
# Part A: Paper evaluation scripts (deterministic, no LLM)
# =====================================================================

# =====================================================================
# Part A note: eval_evolution.py (RPG corpus) removed — eval8 covers
# the same mechanics (teaching, evolution, breeding) using the
# ecosystem creatures.  Old results retained in results/ for reference.
# =====================================================================

# =====================================================================
# Part B: Evolutionary Ecosystem evals (headless, no LLM)
# All use BAAI/bge-base-en-v1.5 embeddings
# =====================================================================

echo "===== Part B: Ecosystem Evals (headless, no LLM) ====="
echo ""

# ----- §11.10 Population Dynamics -----
echo "--- Eval 1: Population Dynamics (200+ generations, ~5 min) ---"
python3 "$ECO_DIR/eval1_population_dynamics.py"
echo ""

# ----- §11.11 Dual-Pathway Ablation -----
echo "--- Eval 2: Dual-Pathway Ablation (BEAR on vs off, ~5 min) ---"
python3 "$ECO_DIR/eval2_dual_pathway_ablation.py"
echo ""

# ----- §11.12 Inheritance Fidelity -----
echo "--- Eval 3: Inheritance Fidelity (~10 min) ---"
python3 "$ECO_DIR/eval3_inheritance_fidelity.py"
echo ""

# ----- §11.12b Locus-Based Breeding -----
echo "--- Eval 3b: Locus-Based Breeding Comparison (~10 min) ---"
python3 "$ECO_DIR/eval3b_locus_breeding.py"
echo ""

# ----- §11.13 Epoch Phenotype Shift -----
echo "--- Eval 4: Epoch Phenotype Shift (~10 min) ---"
python3 "$ECO_DIR/eval4_epoch_phenotype_shift.py"
echo ""

# ----- §11.14 GA Baseline -----
echo "--- Eval 5: BEAR vs Numeric GA (~5 min) ---"
python3 "$ECO_DIR/eval5_ga_baseline.py"
echo ""

# ----- §11.15 Retrieval Diversity -----
echo "--- Eval 6: Dialogue/Retrieval Diversity (~2 min) ---"
python3 "$ECO_DIR/eval6_dialogue_quality.py"
echo ""

# ----- §11.16 Mutation Diversity -----
echo "--- Eval 7: Mutation Diversity (~2 min) ---"
python3 "$ECO_DIR/eval7_mutation_diversity.py"
echo ""

# ----- §11.17 Evolution Dynamics (Teaching + Autonomous + Breeding) -----
echo "--- Eval 8: Evolution Dynamics (~10 min) ---"
python3 "$ECO_DIR/eval8_evolution_dynamics.py"
echo ""

# ----- §11.18 Diploid Diversity -----
echo "--- Eval 8 Diploid: Diploid vs Haploid Diversity (~5 min) ---"
python3 "$ECO_DIR/eval8_diploid_diversity.py"
echo ""

# ----- §11.19 Diploid Under Selection Pressure -----
# Requires scipy for statistical tests
echo "--- Eval 9: Diploid Under Selection Pressure (~30 min) ---"
python3 "$ECO_DIR/eval9_diploid_selection.py"
echo ""

# =====================================================================
# Part C: LLM-mediated evals (require local LLM or Anthropic API)
# =====================================================================

if [[ "$ALL" == true ]]; then
    echo "===== Part C: LLM-Mediated Evals ====="
    if [[ -n "$BASE_URL" ]]; then
        echo "  Using: $BASE_URL (model: ${MODEL:-auto-detect})"
    elif [[ -n "$ANTHROPIC_API_KEY" ]] || [[ "$BACKEND" == "anthropic" ]]; then
        echo "  Using: Anthropic API (Claude Haiku)"
    else
        echo "  Using: LM Studio at localhost:1234"
        echo "  Tip: use --base-url http://localhost:11434/v1 for Ollama"
    fi
    echo ""

    # ----- §11.15b LLM Output Diversity -----
    # ~160 LLM calls, ~5 min
    echo "--- Eval 6b: LLM Dialogue Diversity (~5 min) ---"
    python3 "$ECO_DIR/eval6b_llm_dialogue.py" $MODEL_ARGS
    echo ""

    # ----- §11.16b LLM Mutation Diversity -----
    # ~1480 LLM calls, ~15-20 min
    echo "--- Eval 7b: LLM Mutation Diversity (~15-20 min) ---"
    python3 "$ECO_DIR/eval7b_llm_mutation.py" $MODEL_ARGS
    echo ""

    # ----- §11.14b LLM Breeding Simulation -----
    # ~8 LLM calls per breed event, ~10-30 min
    echo "--- Eval 5b: LLM Breeding Simulation (~10-30 min) ---"
    python3 "$ECO_DIR/eval5b_llm_breeding.py" $MODEL_ARGS
    echo ""
else
    echo "===== Skipping LLM-mediated evals (use --all to include) ====="
    echo "  eval6b_llm_dialogue.py   (needs ANTHROPIC_API_KEY or LM Studio)"
    echo "  eval7b_llm_mutation.py   (needs ANTHROPIC_API_KEY or LM Studio)"
    echo "  eval5b_llm_breeding.py   (needs ANTHROPIC_API_KEY or LM Studio)"
    echo ""
fi

echo "========================================"
echo "  Paper 1 evals complete"
echo "  Paper results in:    $RESULTS_DIR/"
echo "  Ecosystem results in: $ECO_DIR/results/"
echo "========================================"
