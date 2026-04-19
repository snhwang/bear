"""
run_demo_session_v2.py — Reruns all 8 topics with 100,000-char PDF extraction
and an additional 3rd paper for the 5 original 2-paper topics.

New papers to download before running:
  DMG/     ONC201_JCO_2024.pdf      — https://pmc.ncbi.nlm.nih.gov/articles/PMC11095894/
  stroke/  WSO_AF_prevention.pdf    — https://pmc.ncbi.nlm.nih.gov/articles/PMC11951358/
  MS/      MS_biomarkers_2024.pdf   — https://pmc.ncbi.nlm.nih.gov/articles/PMC11496965/
  alzheimers/ lecanemab_donanemab_eNeuro.pdf — https://pmc.ncbi.nlm.nih.gov/articles/PMC11218032/
  epilepsy/  ketogenic_diet_mechanisms.pdf   — https://pmc.ncbi.nlm.nih.gov/articles/PMC10914588/

Output: session logs saved with _v2 timestamp suffix (no overwrite of v1 data).
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).parent

# ── Session scripts: 7 facilitator prompts + 3 PDFs for all topics ──────────
# All topics now use 3 PDFs (consistent protocol)
SCRIPTS = {
    "dmg": [
        ("wait_ready",),
        ("chat", "Let's discuss recent advances in the treatment of diffuse midline glioma, particularly H3K27M-mutant tumors."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Liu et al adaptive immunotherapeutic paradigms in DMG",
            "pdf_path": str(HERE / "DMG" / "Liu et al adaptive immunotherapeutic paradigms in DMG.pdf"),
        }),
        ("chat", "What do the latest clinical data tell us about immunotherapy approaches — CAR-T, checkpoint inhibitors, and ONC201 — for DMG?"),
        ("chat", "Which of these approaches has the strongest efficacy evidence, and what are the key safety concerns?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Nonnenbroich DMG from molecular mechanisms to targeted interventions",
            "pdf_path": str(HERE / "DMG" / "Nonnenbroich DMG from molecular mechanisms to targeted interventions.pdf"),
        }),
        ("chat", "How do the molecular mechanisms — H3K27M, epigenetic reprogramming, tumor microenvironment — inform which targeted therapies are most promising?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Arrillaga-Romany ONC201 dordaviprone H3K27M DMG",
            "pdf_path": str(HERE / "DMG" / "arrillaga-romany ONC201 (Dordaviprone).pdf"),
        }),
        ("chat", "Given the ONC201 clinical trial data, what combination strategies and patient selection criteria seem most promising for the next generation of trials?"),
        ("chat", "What would a realistic near-term clinical trial design look like for H3K27M-mutant DMG?"),
        ("done",),
    ],

    "stroke": [
        ("wait_ready",),
        ("chat", "Let's discuss the current state of stroke treatment and prevention, from acute intervention to long-term secondary prevention."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Ding thrombectomy new standard of care J Stroke 2015",
            "pdf_path": str(HERE / "stroke" / "thrombectomy.pdf"),
        }),
        ("chat", "Mechanical thrombectomy has transformed acute stroke care. What are its current limitations and how might they be addressed?"),
        ("chat", "Beyond thrombectomy, what neuroprotective strategies show the most promise in the acute phase?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Haupt neuroprotective strategies for ischemic stroke IJMS 2023",
            "pdf_path": str(HERE / "stroke" / "neuroprotection.pdf"),
        }),
        ("chat", "Shifting to secondary prevention — what does the evidence say about anticoagulation strategies, especially for AF-related stroke?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Sposato ischemic stroke prevention atrial fibrillation",
            "pdf_path": str(HERE / "stroke" / "sposato ischemic stroke prevention.pdf"),
        }),
        ("chat", "What are the most important unresolved questions in stroke secondary prevention, and how should future trials be designed?"),
        ("chat", "How do we synthesize these findings into a coherent strategy for a patient presenting with AF-related stroke today?"),
        ("done",),
    ],

    "ms": [
        ("wait_ready",),
        ("chat", "Let's discuss the current treatment landscape and emerging directions for multiple sclerosis, including both relapsing and progressive forms."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Alping rituximab disease-modifying therapy MS Basic Clin Pharmacol Toxicol 2023",
            "pdf_path": str(HERE / "MS" / "DMT.pdf"),
        }),
        ("chat", "Rituximab and other B-cell depleting therapies have shown strong efficacy. What are the key debates around off-label use versus approved agents?"),
        ("chat", "Progressive MS remains the major unmet need. What mechanisms drive progression independently of relapses?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "De Keersmaecker remyelination disease-modifying treatments MS Eur J Neurol 2025",
            "pdf_path": str(HERE / "MS" / "remyelination.pdf"),
        }),
        ("chat", "Which DMTs show genuine remyelination potential, and how do we distinguish neuroprotection from true repair?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Anderhalten emerging MRI and biofluid biomarkers MS",
            "pdf_path": str(HERE / "MS" / "anderhalten emerging MRI and biofluid biomarkers.pdf"),
        }),
        ("chat", "How can emerging biomarkers — NfL, paramagnetic rim lesions, kappa free light chains — change how we monitor and treat MS?"),
        ("chat", "What does a personalized MS treatment algorithm look like in 2025, integrating DMTs, biomarkers, and remyelination strategies?"),
        ("done",),
    ],

    "alzheimers": [
        ("wait_ready",),
        ("chat", "Let's discuss Alzheimer's disease — from its pathological mechanisms to the latest disease-modifying therapies."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Scarano Alzheimer's disease etiology hypotheses therapeutic strategies IJMS 2025",
            "pdf_path": str(HERE / "alzheimers" / "Alzheimer's disease etiology hypotheses and therapeutic strategies.pdf"),
        }),
        ("chat", "The amyloid cascade hypothesis has dominated AD research. What are the strongest competing hypotheses, and how do they shape therapeutic strategy?"),
        ("chat", "Lecanemab and donanemab received FDA approval. What does the clinical evidence actually show about their real-world benefit?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Hardy Alzheimer's disease treatment challenges for the future J Neurochem 2025",
            "pdf_path": str(HERE / "alzheimers" / "Alzheimer's disease treatment challenges for the future.pdf"),
        }),
        ("chat", "What are the biggest barriers to translating anti-amyloid therapy into broad clinical benefit — ARIA risks, cost, access, patient selection?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Espay lecanemab and donanemab illustrated perspective",
            "pdf_path": str(HERE / "alzheimers" / "espay lecanemab and donanemab.pdf"),
        }),
        ("chat", "Beyond amyloid — tau, neuroinflammation, synaptic loss — which targets are most promising for the next generation of AD therapies?"),
        ("chat", "How should the field prioritize research and development given the modest clinical benefits and high costs of current anti-amyloid therapies?"),
        ("done",),
    ],

    "epilepsy": [
        ("wait_ready",),
        ("chat", "Let's discuss epilepsy — from individualized seizure risk prediction to emerging therapeutic approaches including gene therapy and dietary interventions."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Schubert epilepsy as a dynamic disease Epilepsia 2025",
            "pdf_path": str(HERE / "epilepsy" / "Epilepsy as a dynamic disease.pdf"),
        }),
        ("chat", "Seizure risk prediction is increasingly individualized. What data and methods are most promising for actionable risk models in clinical practice?"),
        ("chat", "Gene therapy has entered clinical trials for epilepsy. Which genetic epilepsies are most tractable, and what are the key delivery challenges?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Walker state-of-the-art gene therapy epilepsy Curr Opin Neurol 2025",
            "pdf_path": str(HERE / "epilepsy" / "State-of-the-art gene therapy in epilepsy.pdf"),
        }),
        ("chat", "For drug-resistant epilepsy, what is the current evidence for ketogenic diet therapy — who benefits most, and what are the mechanistic explanations?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Mishra drug resistant epilepsy and ketogenic diet",
            "pdf_path": str(HERE / "epilepsy" / "mishra drug resistant epilepsy and ketogenic diet.pdf"),
        }),
        ("chat", "How do we integrate gene therapy, dietary intervention, and traditional ASMs into a personalized treatment algorithm for drug-resistant epilepsy?"),
        ("chat", "What are the most important knowledge gaps, and how should clinical trial design evolve to address them?"),
        ("done",),
    ],

    # 3-paper topics unchanged (already had 3 PDFs and 7 prompts)
    "glp1": [
        ("wait_ready",),
        ("chat", "Let's discuss GLP-1 receptor agonists — their mechanisms, clinical evidence, and evolving role beyond diabetes and obesity."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Uriti systemic effect of GLP-1 receptor agonists",
            "pdf_path": str(HERE / "GLP-1" / "Uriti systemic effect of GLP-1.pdf"),
        }),
        ("chat", "What systemic effects of GLP-1 agonists — cardiovascular, renal, neurological — have the strongest evidence, and what are the proposed mechanisms?"),
        ("chat", "What are the main safety concerns and contraindications, and how should we weigh risk vs. benefit in different patient populations?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Deanfield semaglutide cardiovascular outcomes SELECT trial",
            "pdf_path": str(HERE / "GLP-1" / "deanfield semaglutide.pdf"),
        }),
        ("chat", "The SELECT trial showed cardiovascular mortality reduction. How does this change prescribing beyond diabetic patients?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Tzang metabolic rebound after GLP-1 receptor agonist discontinuation",
            "pdf_path": str(HERE / "GLP-1" / "tzang metabolic rebound after GLP-1 receptor agaonist discontinuation.pdf"),
        }),
        ("chat", "Weight regain after discontinuation is a major practical challenge. What strategies — combination therapy, cycling — are most promising?"),
        ("chat", "How do we synthesize the evidence into rational prescribing guidelines for GLP-1 agonists across obesity, cardiometabolic disease, and potentially other indications?"),
        ("done",),
    ],

    "crispr": [
        ("wait_ready",),
        ("chat", "Let's discuss CRISPR gene editing — from its mechanisms to the first FDA-approved therapies and the ethical landscape ahead."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Brokowski CRISPR ethics regulatory framework JMB 2019",
            "pdf_path": str(HERE / "CRISPR" / "CRISPR ethics.pdf"),
        }),
        ("chat", "The ethics of CRISPR — somatic vs. germline editing, equity of access, consent — are hotly debated. What governance frameworks are most important?"),
        ("chat", "Casgevy and Lyfgenia received FDA approval in 2023. What do the clinical data show and what are the remaining limitations?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "FDA approval Casgevy Lyfgenia dual breakthrough Ann Med Surg 2024",
            "pdf_path": str(HERE / "CRISPR" / "fda_approval_of_casgevy_and_lyfgenia__a_dual.10.pdf"),
        }),
        ("chat", "Beyond sickle cell disease and thalassemia, which inherited conditions are the most tractable targets for CRISPR therapy in the next decade?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Laurent CRISPR-based gene therapies preclinical to clinical Cells 2024",
            "pdf_path": str(HERE / "CRISPR" / "laurent CRISPR-based gene therapies.pdf"),
        }),
        ("chat", "What are the most significant technical barriers — delivery, off-target effects, immune responses — and how close are we to solving them?"),
        ("chat", "How should we balance the extraordinary promise of CRISPR with the risks of moving too quickly, especially for germline applications?"),
        ("done",),
    ],

    "llm-cds": [
        ("wait_ready",),
        ("chat", "Let's discuss the role of large language models in clinical decision support — the evidence, the risks, and the path to safe deployment."),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Dennstadt implementing LLMs in healthcare control collaboration costs npj Digital Medicine 2025",
            "pdf_path": str(HERE / "LLMs in Clinical Decision Support" / "dennstadt implementing LLMs in healthcare.pdf"),
        }),
        ("chat", "What clinical tasks are LLMs most and least suited for, based on current evidence? Where is the risk-benefit ratio clearly favorable?"),
        ("chat", "Safety challenges — hallucination, bias, liability — are frequently cited. How serious are these in real clinical deployments?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Wang safety challenges of AI in medicine arXiv 2025",
            "pdf_path": str(HERE / "LLMs in Clinical Decision Support" / "Wang safety challenges of AI in medicine.pdf"),
        }),
        ("chat", "Deterministic frameworks and RAG systems have been proposed to improve reliability. What evidence exists for these approaches?"),
        ("ingest", {
            "hat_id": "white-hat",
            "title": "Arriola-Montenegro deterministic LLM framework clinical decision support Frontiers AI 2025",
            "pdf_path": str(HERE / "LLMs in Clinical Decision Support" / "arriola-omontenegro deterministic LLM framework.pdf"),
        }),
        ("chat", "What regulatory and governance frameworks are needed for safe clinical AI deployment, and how should clinicians be trained to work with these systems?"),
        ("chat", "Looking 5 years ahead — what does responsible, evidence-based integration of LLMs into clinical practice look like?"),
        ("done",),
    ],
}


SERVER_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws"


async def wait_for_server(timeout: int = 180) -> None:
    """Poll until the parlor server is accepting connections."""
    import aiohttp
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(SERVER_URL, timeout=aiohttp.ClientTimeout(total=2)):
                    return
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError("Parlor server did not start in time")


async def ingest_pdf_http(hat_id: str, pdf_path: str) -> None:
    """POST a PDF to the running parlor server for ingestion."""
    import aiohttp
    async with aiohttp.ClientSession() as s:
        with open(pdf_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("hat_id", hat_id)
            data.add_field("file", f, filename=os.path.basename(pdf_path),
                           content_type="application/pdf")
            async with s.post(f"{SERVER_URL}/ingest", data=data) as resp:
                result = await resp.json()
                print(f"  [Ingest response] {result}")
                if not result.get("success"):
                    print(f"  [Ingest error] {result}")


async def run_ws_script(script: list) -> int:
    """Execute the scripted session over WebSocket. Returns turn count."""
    import aiohttp
    turn_count = 0

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            async def reader():
                nonlocal turn_count
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        import json as _j
                        data = _j.loads(msg.data)
                        if data.get("type") == "message":
                            sender = data.get("sender_name", "?")
                            snippet = data.get("content", "")[:80]
                            turn_count += 1
                            print(f"  [{turn_count}] {sender}: {snippet}...")
                        elif data.get("type") == "ingest_complete":
                            print(f"  [Ingest complete] {data.get('hat_name')}: "
                                  f"{data.get('count')} chunks from {data.get('source')}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.ERROR):
                        break

            reader_task = asyncio.create_task(reader())
            await asyncio.sleep(3)

            import json as _json
            for step in script:
                action = step[0]
                if action == "wait_ready":
                    print("Waiting for initial activity to settle...")
                    await asyncio.sleep(10)
                elif action == "chat":
                    payload = step[1]
                    print(f"\n>>> USER: {payload}")
                    await ws.send_str(_json.dumps({"type": "chat", "content": payload}))
                    print("  Waiting 50s for responses...")
                    await asyncio.sleep(50)
                elif action == "ingest":
                    info = step[1]
                    hat_id = info["hat_id"]
                    pdf_path = info["pdf_path"]
                    print(f"\n>>> INJECTING PDF to {hat_id}: {os.path.basename(pdf_path)}")
                    await ingest_pdf_http(hat_id, pdf_path)
                    print("  Waiting 15s for ingestion reaction...")
                    await asyncio.sleep(15)
                elif action == "done":
                    print(f"\n=== Session complete. {turn_count} turns captured. ===")
                    break

            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

    return turn_count


async def run_session(topic: str, condition: str = "bear") -> None:
    """Start parlor server, run one session, then shut down."""
    import signal
    import subprocess

    script = SCRIPTS[topic]
    naive = condition == "naive"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"\n{'='*60}")
    print(f"Starting: {topic} / {condition} / {timestamp}")
    print(f"{'='*60}")

    cmd = [
        sys.executable, "-u", "parlor.py",
        "--panel", "brainstorming-hats",
        "--backend", "anthropic",
        "--topic-meta", topic,
        "--condition-meta", condition,
    ]
    if naive:
        cmd.append("--naive-diffusion")

    # Wipe knowledge store for a clean evaluation run
    import shutil, platform, os
    is_wsl = "microsoft" in platform.uname().release.lower() or \
             os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
    if is_wsl:
        import tempfile
        kb_path = Path(tempfile.gettempdir()) / "bear_knowledge" / "brainstorming-hats"
    else:
        kb_path = HERE / "panel_data" / "knowledge"
    if kb_path.exists():
        shutil.rmtree(kb_path)
        print(f"  Wiped knowledge store: {kb_path}")

    print("Starting BEAR Parlor server...", flush=True)
    server = subprocess.Popen(cmd, cwd=str(HERE))

    try:
        await wait_for_server(timeout=180)
        await asyncio.sleep(2)
        await run_ws_script(script)
    finally:
        print("\nShutting down server (log_session_summary will be written)...")
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        print("Done.")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run v2 demo sessions")
    parser.add_argument("--topic", choices=list(SCRIPTS.keys()) + ["all"],
                        default="all")
    parser.add_argument("--condition", choices=["bear", "naive", "both"],
                        default="both")
    parser.add_argument("--topics", nargs="+", choices=list(SCRIPTS.keys()),
                        help="Run specific topics")
    parser.add_argument("--rerun-incomplete", action="store_true",
                        help="Only rerun sessions missing .knowledge.json or completed=None")
    args = parser.parse_args()

    topics = args.topics or (
        list(SCRIPTS.keys()) if args.topic == "all" else [args.topic]
    )
    conditions = (
        ["bear", "naive"] if args.condition == "both" else [args.condition]
    )

    # --rerun-incomplete: filter to only sessions missing .knowledge.json or not completed
    if args.rerun_incomplete:
        import json as _json
        LOG_DIR = HERE / "session_logs"
        _raw = []
        for _f in sorted(LOG_DIR.glob("brainstorming-hats_*.stats.json")):
            _d = _json.load(open(_f))
            if not _d.get("topic") or not _d.get("n_turns"): continue
            _ts = _f.name.replace("brainstorming-hats_","").replace(".stats.json","")
            _raw.append((_ts, _d))
        _seen = {}
        for _ts, _d in _raw:
            _key = (_d["topic"], _d["condition"])
            _kj = (LOG_DIR / f"brainstorming-hats_{_ts}.knowledge.json").exists()
            _done = _d.get("completed") is True
            if _key not in _seen:
                _seen[_key] = (_ts, _d)
            else:
                # Prefer: (1) has kj + completed, (2) has kj, (3) most turns
                _prev_ts, _prev_d = _seen[_key]
                _prev_kj = (LOG_DIR / f"brainstorming-hats_{_prev_ts}.knowledge.json").exists()
                _prev_done = _prev_d.get("completed") is True
                _cur_score  = (int(_kj and _done), int(_kj), _d["n_turns"])
                _prev_score = (int(_prev_kj and _prev_done), int(_prev_kj), _prev_d["n_turns"])
                if _cur_score > _prev_score:
                    _seen[_key] = (_ts, _d)
        pairs = []
        for t in topics:
            for c in conditions:
                _ts, _d = _seen.get((t, c), (None, None))
                if not _d:
                    pairs.append((t, c)); continue
                _kj = (LOG_DIR / f"brainstorming-hats_{_ts}.knowledge.json").exists()
                if not _kj or _d.get("completed") is not True:
                    pairs.append((t, c))
        print(f"Rerun mode: {len(pairs)} incomplete sessions")
        for t, c in pairs: print(f"  {t}/{c}")
    else:
        pairs = [(t, c) for t in topics for c in conditions]
        print(f"Running {len(topics)} topic(s) × {len(conditions)} condition(s)")
        print(f"Topics: {topics}")
        print(f"Conditions: {conditions}")
    print(f"\nEnsure these PDFs exist before running:")
    new_pdfs = [
        "DMG/arrillaga-romany ONC201 (Dordaviprone).pdf",
        "stroke/sposato ischemic stroke prevention.pdf",
        "MS/anderhalten emerging MRI and biofluid biomarkers.pdf",
        "alzheimers/espay lecanemab and donanemab.pdf",
        "epilepsy/mishra drug resistant epilepsy and ketogenic diet.pdf",
    ]
    for pdf in new_pdfs:
        path = HERE / pdf
        status = "✓" if path.exists() else "✗ MISSING"
        print(f"  {status}  {pdf}")

    print()

    for topic, condition in pairs:
        await run_session(topic, condition)

    print("\nAll sessions complete.")


if __name__ == "__main__":
    asyncio.run(main())
