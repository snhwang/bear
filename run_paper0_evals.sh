#!/usr/bin/env bash
# =============================================================================
# run_paper0_evals.sh — Retrieval-Governed Prompting (tool_retrieval_paper.tex)
#
# Paper 0 thesis: dynamic retrieval beats static prompt construction.
# Evaluates retrieval quality, tool scaling, CPA comparison, token efficiency,
# behavioral divergence, and backend/governance ablation.
#
# LLM REQUIREMENTS:
#   - Most evals are deterministic (no LLM needed)
#   - eval_output_divergence.py requires:
#       LM Studio running with: mistral-nemo-instruct-2407 (Mistral Nemo 12B)
#       OR set ANTHROPIC_API_KEY for Claude Haiku fallback
#   - eval_tool_scaling.py end-to-end dispatch (optional) tries:
#       LM Studio with nvidia/nemotron-3-super at http://127.0.0.1:1234/v1
#       (skips gracefully if unavailable)
#
# EMBEDDING MODELS (downloaded automatically on first use):
#   - BAAI/bge-base-en-v1.5 (768-dim) — primary
#   - Qwen/Qwen3-Embedding-0.6B, Qwen/Qwen3-Embedding-4B — backend comparison
#   - mlx-community variants if on Apple Silicon
#
# Usage:
#   ./run_paper0_evals.sh                              # deterministic only
#   ./run_paper0_evals.sh --all                        # include LLM-dependent evals
#   ./run_paper0_evals.sh --all --model nemotron-super # use specific model
# =============================================================================

set -e
cd "$(dirname "$0")"

# Detect WSL and resolve Windows host IP for LM Studio
if grep -qi microsoft /proc/version 2>/dev/null; then
    WSL_HOST=$(ip route show default 2>/dev/null | awk '/default/{print $3}')
    if [[ -n "$WSL_HOST" ]]; then
        export LM_STUDIO_URL="http://${WSL_HOST}:1234/v1"
    fi
fi

ALL=false
MODEL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all) ALL=true; shift ;;
        --model) MODEL="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

MODEL_ARGS=""
if [[ -n "$MODEL" ]]; then
    MODEL_ARGS="--model $MODEL"
fi

EVAL_DIR="paper/evaluation"
ECO_EVAL_DIR="examples/evolutionary_ecosystem/eval"
RESULTS_DIR="$EVAL_DIR/results"
mkdir -p "$RESULTS_DIR"

echo "========================================"
echo "  Paper 0: Retrieval-Governed Prompting"
echo "========================================"
echo ""

# ----- §4.1 Retrieval Quality -----
echo "--- §4.1 Retrieval Quality ---"
python3 "$EVAL_DIR/eval_retrieval.py" | tee "$RESULTS_DIR/eval_retrieval_output.txt"
echo ""

# ----- §4.1 Retrieval Quality (semantic embeddings) -----
echo "--- §4.1 Retrieval Quality (semantic) ---"
python3 "$EVAL_DIR/eval_retrieval.py" --semantic | tee "$RESULTS_DIR/eval_retrieval_semantic_output.txt"
echo ""

# ----- §4.2 Tool Scaling -----
# Note: end-to-end dispatch step tries LM Studio (nemotron-3-super); skips if unavailable
echo "--- §4.2 Tool Scaling ---"
python3 "$EVAL_DIR/eval_tool_scaling.py" | tee "$RESULTS_DIR/eval_tool_scaling_output.txt"
echo ""

# ----- §4.2 Tool Composition -----
echo "--- §4.2 Tool Composition ---"
python3 "$EVAL_DIR/eval_tool_composition.py" | tee "$RESULTS_DIR/eval_tool_composition_output.txt"
echo ""

# ----- §4.3 CPA Comparison -----
echo "--- §4.3 BEAR vs CPA Baseline ---"
python3 "$EVAL_DIR/eval_baseline_comparison.py" | tee "$RESULTS_DIR/eval_baseline_output.txt"
echo ""

# ----- §4.4 Token Efficiency & Scaling -----
echo "--- §4.4 Scalability (10-500 agents) ---"
python3 "$EVAL_DIR/eval_scalability.py" | tee "$RESULTS_DIR/eval_scalability_output.txt"
echo ""

# ----- Parameter sensitivity (supports §4.1, §4.4) -----
echo "--- Parameter Sensitivity (alpha, theta, K) ---"
python3 "$EVAL_DIR/eval_ablation.py" | tee "$RESULTS_DIR/eval_ablation_output.txt"
echo ""

echo "--- Parameter Sensitivity (semantic) ---"
python3 "$EVAL_DIR/eval_ablation.py" --semantic | tee "$RESULTS_DIR/eval_ablation_semantic_output.txt"
echo ""

# ----- §4.5 Behavioral Divergence (retrieval-level, no LLM) -----
echo "--- §4.5 Behavioral Divergence (retrieval-level) ---"
python3 "$EVAL_DIR/eval_divergence.py" | tee "$RESULTS_DIR/eval_divergence_output.txt"
echo ""

echo "--- §4.5 Behavioral Divergence (semantic) ---"
python3 "$EVAL_DIR/eval_divergence.py" --semantic | tee "$RESULTS_DIR/eval_divergence_semantic_output.txt"
echo ""

# ----- §4.5 Refined Query -----
echo "--- Refined Query ---"
python3 "$EVAL_DIR/eval_refined_query.py" | tee "$RESULTS_DIR/eval_refined_output.txt"
echo ""

# ----- §4.6 Backend Comparison & Governance Ablation -----
# These require downloading embedding models (BGE-M3, Qwen3) on first run
echo "--- §4.6 Retrieval Backend Comparison ---"
python3 "$EVAL_DIR/eval_retrieval_backends.py" --all | tee "$RESULTS_DIR/eval_retrieval_backends_output.txt"
echo ""

echo "--- §4.6 Governance Ablation ---"
python3 "$EVAL_DIR/eval_governance_ablation.py" | tee "$RESULTS_DIR/eval_governance_ablation_output.txt"
echo ""

# ----- §4.5 Output Divergence (REQUIRES LLM) -----
# Requires: LM Studio with mistral-nemo-instruct-2407 at http://127.0.0.1:1234/v1
#       OR: ANTHROPIC_API_KEY set for Claude Haiku
if [[ "$ALL" == true ]]; then
    echo "--- §4.5 Output Divergence (LLM required) ---"
    echo "  Expects: LM Studio with mistral-nemo-instruct-2407"
    echo "       OR: ANTHROPIC_API_KEY"
    python3 "$EVAL_DIR/eval_output_divergence.py" $MODEL_ARGS | tee "$RESULTS_DIR/eval_output_divergence_output.txt"
    echo ""

    echo "--- Role Divergence (LLM required) ---"
    echo "  Expects: LM Studio with mistral-nemo-instruct-2407 at localhost:1234"
    python3 "$EVAL_DIR/eval_role_divergence.py" $MODEL_ARGS | tee "$RESULTS_DIR/eval_role_divergence_output.txt"
    echo ""
else
    echo "--- Skipping LLM-dependent evals (use --all to include) ---"
    echo "  eval_output_divergence.py  (needs LM Studio: mistral-nemo-instruct-2407)"
    echo "  eval_role_divergence.py    (needs LM Studio: mistral-nemo-instruct-2407)"
    echo ""
fi

echo "========================================"
echo "  Paper 0 evals complete"
echo "  Results in: $RESULTS_DIR/"
echo "========================================"
