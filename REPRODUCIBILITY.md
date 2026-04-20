# Reproducibility

This library is the **bear** Python package. Paper-specific evaluation scripts,
frozen corpora, and result files live in dedicated artifacts repositories
rather than in this repo, so bear itself stays focused on being a library.

## Paper artifacts repos

Each paper has its own artifacts repo. Clone the relevant one, create a virtual
environment, `pip install -r requirements.txt` (which pins this bear version
via `bear @ git+https://github.com/snhwang/bear.git@v0.1.0`), and run
`./run_evals.sh`.

| Paper | Artifacts repo |
|---|---|
| Retrieval-Governed Context (`tool_retrieval_paper.tex`) | `snhwang/paper-retrieval-governed-context-artifacts` |
| Behavioral Genetics (`bear_behavioral_genetics.tex`) | `snhwang/paper-behavioral-genetics-artifacts` |
| Knowledge Diffusion series (`bear_tiis*.tex`) | `snhwang/paper-knowledge-diffusion-artifacts` |

## Bear version pinning

All three artifact repos pin bear to tag `v0.1.0` (commit `515366e`). Bumping
the bear version will likely change numeric results; update the pin in each
artifacts repo's `requirements.txt` and re-run the full eval suite before
comparing to older results.

## Examples

The `examples/` directory in this repo contains the **runnable** versions of
the demos cited by the papers (pet_sim, bear_parlor, evolutionary_ecosystem,
customer_support), including the live-demo webserver pieces (`app.py`,
`brain.py`) that the artifact repos don't need. To run the ecosystem
simulation live:

```bash
pip install -r examples/evolutionary_ecosystem/requirements.txt
cd examples/evolutionary_ecosystem
python -m server.app --creatures 4
```

## Smoke test

```bash
pip install -e .
python test_lmstudio.py   # minimal bear + LM Studio sanity check
pytest tests/             # library unit tests
```
