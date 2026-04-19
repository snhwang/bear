#!/usr/bin/env bash
# =============================================================================
# run_experiments.sh — Cross-paper experiments (Population, Diffusion)
#
# These experiments test the newer features (Population, Knowledge Diffusion)
# that span multiple papers or are not yet assigned.
#
# ALL REQUIRE LLM ACCESS:
#   Option A: Anthropic API
#     export ANTHROPIC_API_KEY=sk-ant-...
#   Option B: Local LLM (LM Studio or Ollama)
#     LM Studio at http://127.0.0.1:1234/v1
#     OR Ollama at http://127.0.0.1:11434/v1
#
# Usage:
#   ./run_experiments.sh                                    # run all experiments
#   ./run_experiments.sh brainteaser                        # run only brainteaser
#   ./run_experiments.sh medqa                              # run only MedQA
#   ./run_experiments.sh evo                                # run only evolutionary
#   ./run_experiments.sh --model nvidia/nemotron-3-super    # use specific model
# =============================================================================

set -e
cd "$(dirname "$0")"

EXPERIMENT=""
MODEL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        *) EXPERIMENT="$1"; shift ;;
    esac
done

MODEL_ARGS=""
if [[ -n "$MODEL" ]]; then
    MODEL_ARGS="--model $MODEL"
fi
EXP_DIR="experiments"

# Detect WSL and resolve Windows host IP for LM Studio / Ollama
LLM_HOST="127.0.0.1"
if grep -qi microsoft /proc/version 2>/dev/null; then
    WSL_HOST=$(ip route show default 2>/dev/null | awk '/default/{print $3}')
    if [[ -n "$WSL_HOST" ]]; then
        LLM_HOST="$WSL_HOST"
    fi
fi
export LM_STUDIO_URL="http://${LLM_HOST}:1234/v1"

echo "========================================"
echo "  Cross-Paper Experiments"
echo "========================================"
echo ""

# Check for LLM availability
if [[ -n "$ANTHROPIC_API_KEY" ]]; then
    echo "  LLM backend: Anthropic API"
elif curl -s --connect-timeout 3 "http://${LLM_HOST}:1234/v1/models" > /dev/null 2>&1; then
    echo "  LLM backend: LM Studio (${LLM_HOST}:1234)"
elif curl -s --connect-timeout 3 "http://${LLM_HOST}:11434/v1/models" > /dev/null 2>&1; then
    echo "  LLM backend: Ollama (${LLM_HOST}:11434)"
else
    echo "  WARNING: No LLM backend detected!"
    echo "  Set ANTHROPIC_API_KEY or start LM Studio / Ollama"
    echo ""
fi

# ----- Brainteaser Panel Evaluation -----
# Tests Panel effectiveness on lateral reasoning tasks
# Modes: single (one agent), panel (6 hats deliberate), all (both + analyze)
if [[ -z "$EXPERIMENT" || "$EXPERIMENT" == "brainteaser" ]]; then
    echo ""
    echo "--- Brainteaser Panel Evaluation (~30 min) ---"
    echo "  Tests: single-agent vs multi-hat panel on reasoning puzzles"
    echo "  LLM: auto-detect (prefers Claude Sonnet, falls back to local)"
    python3 "$EXP_DIR/brainteaser_eval.py" --mode all --n 50 $MODEL_ARGS
    echo ""
fi

# ----- MedQA Panel Evaluation -----
# Tests Panel effectiveness on medical question answering
# Modes: single, panel, consistency, role-majority, analyze, all
if [[ -z "$EXPERIMENT" || "$EXPERIMENT" == "medqa" ]]; then
    echo ""
    echo "--- MedQA Panel Evaluation (~30 min) ---"
    echo "  Tests: single-agent vs multi-hat panel on medical QA"
    echo "  LLM: auto-detect (prefers Claude Sonnet, falls back to local)"
    python3 "$EXP_DIR/medqa_eval.py" --mode all --n 50 $MODEL_ARGS
    echo ""
fi

# ----- Evolutionary Population Evaluation -----
# Tests Population class with real LLM reasoning over generations
if [[ -z "$EXPERIMENT" || "$EXPERIMENT" == "evo" ]]; then
    echo ""
    echo "--- Evolutionary Population Evaluation (~20 min) ---"
    echo "  Tests: population fitness, allele diversity over 15 generations"
    echo "  LLM: auto-detect"
    python3 "$EXP_DIR/evo_eval.py" --pop 8 --generations 15 --batch 20 $MODEL_ARGS
    echo ""
fi

# ----- Knowledge Diffusion Evaluation -----
# NOTE: This script does not exist yet. It would test the new knowledge
# diffusion feature (prestige/conformist/proximity strategies) with a real
# LLM to measure whether diffusion actually improves population accuracy.
#
# Proposed design:
#   1. exam → baseline accuracy
#   2. exam → diffuse(prestige) → re-exam → measure improvement
#   3. exam → diffuse(conformist) → re-exam → measure improvement
#   4. exam → diffuse(proximity) → re-exam → measure improvement
#
# if [[ -z "$EXPERIMENT" || "$EXPERIMENT" == "diffusion" ]]; then
#     echo ""
#     echo "--- Knowledge Diffusion Evaluation (NOT YET IMPLEMENTED) ---"
#     python3 "$EXP_DIR/diffusion_eval.py" --strategies all --n 30
#     echo ""
# fi

echo "========================================"
echo "  Experiments complete"
echo "  Results in: results/"
echo "========================================"
