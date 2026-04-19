#!/usr/bin/env bash
# =============================================================================
# run_paper2_evals.sh — Cognitive Filtering / TiiS (bear_tiis.tex)
#
# Paper 2 thesis: BEAR instructions govern knowledge acquisition + retention
# in multi-agent deliberation (Six Thinking Hats brainstorming panel).
#
# LLM REQUIREMENTS:
#   - All session-log evals are deterministic (no LLM needed)
#     They analyze pre-recorded session logs from examples/bear_parlor/session_logs/
#   - Generating NEW session logs requires multiple LLM backends:
#       Anthropic API: claude-opus-4-6 (White), claude-sonnet-4-6 (Black), claude-haiku-4-5 (Green)
#       Google Gemini: gemini-3.1-flash-lite-preview (Red), gemini-3.1-flash-image-preview (Blue)
#       LM Studio: nvidia/nemotron-3-super (Yellow)
#   - eval_role_divergence.py requires:
#       LM Studio with: mistral-nemo-instruct-2407 at http://127.0.0.1:1234/v1
#
# EMBEDDING MODELS (downloaded automatically):
#   - BAAI/bge-base-en-v1.5 (768-dim) — all session-log analyses
#
# REQUIRED DATA:
#   - Session logs in examples/bear_parlor/session_logs/
#   - Hat instruction corpora in examples/bear_parlor/instructions/hats/
#
# Usage:
#   ./run_paper2_evals.sh                              # analyze existing session logs (no LLM)
#   ./run_paper2_evals.sh --all                        # include LLM-dependent + new sessions
#   ./run_paper2_evals.sh --all --model nemotron-super # use specific model
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
PARLOR_DIR="examples/bear_parlor"
RESULTS_DIR="$EVAL_DIR/results"
mkdir -p "$RESULTS_DIR"

echo "========================================"
echo "  Paper 2: Cognitive Filtering (TiiS)"
echo "========================================"
echo ""

# Check session logs exist
LOG_COUNT=$(ls "$PARLOR_DIR/session_logs"/brainstorming-hats_*.md 2>/dev/null | wc -l)
if [[ "$LOG_COUNT" -eq 0 ]]; then
    echo "ERROR: No session logs found in $PARLOR_DIR/session_logs/"
    echo "  Generate them first with:"
    echo "    cd $PARLOR_DIR && python3 run_demo_session.py --topic all --clean"
    echo "  This requires: ANTHROPIC_API_KEY, GEMINI_API_KEY, and Ollama (gemma3:4b)"
    exit 1
fi
echo "Found $LOG_COUNT session log(s)"
echo ""

# =====================================================================
# Part A: Session-log analysis (no LLM, uses BAAI/bge-base-en-v1.5)
# =====================================================================

echo "===== Part A: Session Log Analysis (no LLM) ====="
echo ""

# ----- Inter-Hat Differentiation -----
echo "--- Inter-Hat Differentiation (centroid distances) ---"
python3 "$EVAL_DIR/eval_interhat_differentiation.py" | tee "$RESULTS_DIR/eval_interhat_output.txt"
echo ""

# ----- Role Adherence -----
echo "--- Role Adherence (self-alignment, discrimination ratio) ---"
python3 "$EVAL_DIR/eval_role_adherence.py" | tee "$RESULTS_DIR/eval_role_adherence_output.txt"
echo ""

# ----- Statistical Significance -----
# Requires scipy
echo "--- Statistical Significance (t-tests, Wilcoxon, bootstrap, Holm-Bonferroni) ---"
python3 "$EVAL_DIR/eval_significance.py" | tee "$RESULTS_DIR/eval_significance_output.txt"
echo ""

# ----- Embed-Only Baseline -----
echo "--- Embed-Only Baseline (naive vs embed-only vs BEAR dedup) ---"
python3 "$EVAL_DIR/eval_embed_only_baseline.py" | tee "$RESULTS_DIR/eval_embed_only_output.txt"
echo ""

# ----- d_min Sensitivity -----
echo "--- d_min Sensitivity Sweep (0.20 - 0.50) ---"
python3 "$EVAL_DIR/eval_dmin_sensitivity.py" | tee "$RESULTS_DIR/eval_dmin_output.txt"
echo ""

# ----- Temporal Evolution -----
echo "--- Temporal Store Evolution (growth over turns) ---"
python3 "$EVAL_DIR/eval_temporal_evolution.py" | tee "$RESULTS_DIR/eval_temporal_output.txt"
echo ""

# ----- Response Divergence -----
echo "--- Response Divergence (BEAR vs Naive inter-hat response distance) ---"
python3 "$EVAL_DIR/eval_response_divergence.py" | tee "$RESULTS_DIR/eval_response_divergence_output.txt"
echo ""

# =====================================================================
# Part B: LLM-dependent evals + session generation
# =====================================================================

if [[ "$ALL" == true ]]; then
    echo "===== Part B: LLM-Dependent Evals ====="
    echo ""

    # ----- Role Divergence -----
    # Requires: LM Studio with mistral-nemo-instruct-2407 at localhost:1234
    echo "--- Role Divergence (BEAR vs Role vs Static prompt) ---"
    echo "  Expects: LM Studio with mistral-nemo-instruct-2407 at localhost:1234"
    python3 "$EVAL_DIR/eval_role_divergence.py" $MODEL_ARGS | tee "$RESULTS_DIR/eval_role_divergence_output.txt"
    echo ""

    # ----- Generate New Session Logs (optional) -----
    # Uncomment to re-generate session logs from scratch.
    # Requires ALL of:
    #   ANTHROPIC_API_KEY — Claude Opus (White), Sonnet (Black), Haiku (Green)
    #   GEMINI_API_KEY    — Gemini Flash Lite (Red), Flash Image (Blue)
    #   Ollama running    — gemma3:4b (Yellow)
    #
    # echo "--- Generating BEAR-guided sessions ---"
    # cd "$PARLOR_DIR"
    # python3 run_demo_session.py --topic all --clean
    #
    # echo "--- Generating Naive sessions ---"
    # python3 run_demo_session.py --topic all --naive-diffusion --clean
    #
    # echo "--- Generating Constant-Model Control (all claude-sonnet-4-6) ---"
    # cp characters.yaml characters.yaml.bak
    # python3 run_demo_session.py --topic dmg --clean --backend anthropic --model claude-sonnet-4-6
    # python3 run_demo_session.py --topic dmg --naive-diffusion --clean --backend anthropic --model claude-sonnet-4-6
    # cp characters.yaml.bak characters.yaml
    # cd -
    # echo ""
else
    echo "===== Skipping LLM-dependent evals (use --all to include) ====="
    echo "  eval_role_divergence.py  (needs LM Studio: mistral-nemo-instruct-2407)"
    echo ""
    echo "  To generate new session logs, uncomment the session generation"
    echo "  block in this script. Requires:"
    echo "    ANTHROPIC_API_KEY — Claude Opus/Sonnet/Haiku"
    echo "    GEMINI_API_KEY    — Gemini Flash Lite/Image"
    echo "    Ollama running    — gemma3:4b"
    echo ""
fi

echo "========================================"
echo "  Paper 2 evals complete"
echo "  Results in: $RESULTS_DIR/"
echo "========================================"
