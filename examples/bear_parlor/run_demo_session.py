#!/usr/bin/env python3
"""
Scripted demo session runner for BEAR Parlor brainstorming panel.

Starts the parlor server, connects via WebSocket, sends user messages
and injects PDFs at controlled points, then waits for the session log
to capture enough turns (including diffusion events).

Usage:
    python run_demo_session.py                          # DMG topic, BEAR-guided
    python run_demo_session.py --topic stroke            # stroke topic
    python run_demo_session.py --topic ms                # multiple sclerosis topic
    python run_demo_session.py --naive-diffusion         # ablation mode
    python run_demo_session.py --topic all               # run all 3 topics sequentially

The session log will be saved to session_logs/.
"""
from __future__ import annotations

import functools
import builtins
builtins.print = functools.partial(builtins.print, flush=True)

import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
SERVER_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws"

# Parse args early so SCRIPT and server launch can reference them
import argparse as _ap
_parser = _ap.ArgumentParser(description="Scripted BEAR Parlor demo session")
_parser.add_argument(
    "--naive-diffusion", action="store_true",
    help="Run with naive diffusion (no BEAR filtering or dedup) for ablation.",
)
_parser.add_argument(
    "--topic", default="dmg",
    choices=["dmg", "stroke", "ms", "alzheimers", "epilepsy",
             "glp1", "crispr", "llm-cds", "all"],
    help="Brainstorming topic (default: dmg). Use 'all' to run all topics sequentially.",
)
_parser.add_argument(
    "--backend", default="anthropic",
    help="Default LLM backend for session tasks (diffusion, insights, summaries). "
         "Default: anthropic. Per-hat overrides in characters.yaml still apply.",
)
_parser.add_argument(
    "--model", default=None,
    help="Default model name for the session backend.",
)
_parser.add_argument(
    "--clean", action="store_true",
    help="Wipe the ChromaDB knowledge store before each topic for clean evaluation.",
)
_cli_args = _parser.parse_args()

# ---------------------------------------------------------------------------
# Topic-specific scripts
# ---------------------------------------------------------------------------
# Each script is a list of (action, payload, wait_seconds_after)
# Actions: "wait_ready", "chat", "ingest", "done"

SCRIPTS = {
    "dmg": [
        ("wait_ready", None, 8),

        # Phase 1: Open topic, let hats establish positions
        ("chat", "Discuss possible new treatments for diffuse midline glioma", 40),

        # Inject first PDF — immunotherapy focus
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "DMG" / "Liu et al adaptive immunotherapeutic paradigms in DMG.pdf"),
        }, 15),

        # Phase 2: Steer toward immunotherapy — triggers paper-informed responses
        ("chat", "What about immunotherapy approaches like CAR-T or checkpoint inhibitors for DMG?", 50),

        # Phase 3: Push for evidence ranking — forces hats to weigh what they've learned
        ("chat", "Which of these approaches has the strongest evidence so far?", 50),

        # Inject second PDF — molecular mechanisms
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "DMG" / "Nonnenbroich DMG from molecular mechanisms to targeted interventions.pdf"),
        }, 15),

        # Phase 4: Pivot to molecular targeting — new diffusion wave from second paper
        ("chat", "How do the molecular mechanisms of DMG inform which targeted therapies might work?", 50),

        # Phase 5: Synthesis — hats should draw on accumulated diffused knowledge
        ("chat", "What combination strategies seem most promising given everything we've discussed?", 50),

        # Phase 6: Practical next steps
        ("chat", "What would a realistic clinical trial design look like for the most promising approach?", 50),

        ("done", None, 0),
    ],

    "stroke": [
        ("wait_ready", None, 8),

        # Phase 1: Open topic
        ("chat", "Discuss new treatment approaches for acute ischemic stroke", 40),

        # Inject first PDF — thrombectomy
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "stroke" / "thrombectomy.pdf"),
        }, 15),

        # Phase 2: Steer toward interventional approaches
        ("chat", "How has mechanical thrombectomy changed stroke outcomes, and what are its limitations?", 50),

        # Phase 3: Time window and patient selection
        ("chat", "What about extending the treatment window beyond 24 hours? Is there evidence for late intervention?", 50),

        # Inject second PDF — neuroprotection
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "stroke" / "neuroprotection.pdf"),
        }, 15),

        # Phase 4: Pivot to neuroprotection — new angle
        ("chat", "Can neuroprotective agents complement thrombectomy, or have they all failed in trials?", 50),

        # Phase 5: Synthesis
        ("chat", "What combination of acute intervention and neuroprotection seems most promising?", 50),

        # Phase 6: Practical considerations
        ("chat", "What are the biggest barriers to implementing these approaches in community hospitals?", 50),

        ("done", None, 0),
    ],

    "ms": [
        ("wait_ready", None, 8),

        # Phase 1: Open topic
        ("chat", "Discuss emerging treatment strategies for multiple sclerosis", 40),

        # Inject first PDF — disease-modifying therapies
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "MS" / "DMT.pdf"),
        }, 15),

        # Phase 2: Current landscape
        ("chat", "How do the newer disease-modifying therapies compare to older ones in terms of efficacy and safety?", 50),

        # Phase 3: Treatment escalation
        ("chat", "Should we treat aggressively from the start or escalate? What does the evidence say?", 50),

        # Inject second PDF — remyelination
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "MS" / "remyelination.pdf"),
        }, 15),

        # Phase 4: Pivot to repair — new angle
        ("chat", "What about remyelination therapies? Can we actually repair damage rather than just prevent it?", 50),

        # Phase 5: Synthesis
        ("chat", "How might we combine immunomodulation with repair strategies for a more complete treatment approach?", 50),

        # Phase 6: Progressive MS
        ("chat", "What about progressive MS specifically — are any of these approaches viable there?", 50),

        ("done", None, 0),
    ],

    "alzheimers": [
        ("wait_ready", None, 8),

        # Phase 1: Open topic
        ("chat", "Discuss promising new therapeutic strategies for Alzheimer's disease", 40),

        # Inject first PDF — etiology and therapeutic strategies
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "alzheimers" / "Alzheimer's disease etiology hypotheses and therapeutic strategies.pdf"),
        }, 15),

        # Phase 2: Steer toward mechanisms
        ("chat", "Beyond amyloid-beta, what other pathological mechanisms are being targeted for Alzheimer's treatment?", 50),

        # Phase 3: Push for evidence evaluation
        ("chat", "Which of these non-amyloid approaches has the strongest clinical evidence so far?", 50),

        # Inject second PDF — treatment challenges
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "alzheimers" / "Alzheimer's disease treatment challenges for the future.pdf"),
        }, 15),

        # Phase 4: Pivot to challenges and barriers
        ("chat", "What are the biggest challenges preventing successful Alzheimer's drug development?", 50),

        # Phase 5: Synthesis
        ("chat", "How might combination therapies or multi-target approaches address these challenges?", 50),

        # Phase 6: Future directions
        ("chat", "What role could early detection and prevention play compared to treating established disease?", 50),

        ("done", None, 0),
    ],

    "epilepsy": [
        ("wait_ready", None, 8),

        # Phase 1: Open topic
        ("chat", "Discuss novel treatment approaches for drug-resistant epilepsy", 40),

        # Inject first PDF — epilepsy as dynamic disease
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "epilepsy" / "Epilepsy as a dynamic disease.pdf"),
        }, 15),

        # Phase 2: Steer toward dynamic mechanisms
        ("chat", "How does viewing epilepsy as a dynamic disease change our approach to treatment?", 50),

        # Phase 3: Push for specific interventions
        ("chat", "What neuromodulation or closed-loop stimulation approaches show the most promise?", 50),

        # Inject second PDF — gene therapy
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "epilepsy" / "State-of-the-art gene therapy in epilepsy.pdf"),
        }, 15),

        # Phase 4: Pivot to gene therapy
        ("chat", "Can gene therapy realistically cure certain forms of epilepsy? What are the current results?", 50),

        # Phase 5: Synthesis
        ("chat", "How might gene therapy, neuromodulation, and precision medicine be combined for drug-resistant epilepsy?", 50),

        # Phase 6: Practical considerations
        ("chat", "What are the biggest barriers to translating these approaches from research to clinical practice?", 50),

        ("done", None, 0),
    ],

    # --- New topics (varied facilitator arcs, 20+ turns, 2-3 PDFs each) ---

    "glp1": [
        ("wait_ready", None, 8),

        # Phase 1: Start with a clinical case — ground the discussion
        ("chat", "A 52-year-old obese patient with type 2 diabetes asks about semaglutide. "
                 "What should we consider when evaluating GLP-1 receptor agonists for this patient?", 50),

        # Inject first PDF — systemic effects
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "GLP-1" / "Uriti systemic effect of GLP-1.pdf"),
        }, 15),

        # Phase 2: Mechanism — what does the evidence say?
        ("chat", "Beyond glycemic control, what systemic effects do GLP-1 receptor agonists have? "
                 "Are the cardiovascular and renal benefits real or overstated?", 50),

        # Phase 3: Push for risk assessment
        ("chat", "What are the serious risks and side effects we should be worried about with long-term GLP-1 RA use?", 50),

        # Inject second PDF — semaglutide cardiovascular trial
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "GLP-1" / "deanfield semaglutide.pdf"),
        }, 15),

        # Phase 4: Pivot to cardiovascular outcomes
        ("chat", "How strong is the cardiovascular evidence for semaglutide specifically? "
                 "Does it hold up across different patient populations?", 50),

        # Inject third PDF — metabolic rebound
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "GLP-1" / "tzang metabolic rebound after GLP-1 receptor agaonist discontinuation.pdf"),
        }, 15),

        # Phase 5: Controversial question — weight regain on discontinuation
        ("chat", "Patients regain weight when they stop GLP-1 agonists. Does this mean we're committing patients "
                 "to lifelong treatment? Is that ethical and sustainable?", 60),

        # Phase 6: Contested question — pediatric use
        ("chat", "Should we prescribe semaglutide to teenagers with obesity? What are the arguments for and against?", 60),

        # Phase 7: Synthesis — clinical decision framework
        ("chat", "Given everything discussed, what decision framework should clinicians use when considering "
                 "GLP-1 RAs — which patients benefit most and when should we avoid them?", 50),

        ("done", None, 0),
    ],

    "crispr": [
        ("wait_ready", None, 8),

        # Phase 1: Start with controversy — ethics first
        ("chat", "CRISPR gene editing raises profound ethical questions. Where should we draw the line "
                 "between therapeutic editing and enhancement? Should germline editing ever be permitted?", 50),

        # Inject first PDF — ethics
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "CRISPR" / "CRISPR ethics.pdf"),
        }, 15),

        # Phase 2: Dig into the ethical framework
        ("chat", "What ethical frameworks are being applied to CRISPR governance? "
                 "Are the current regulations adequate, or are we falling behind the technology?", 50),

        # Phase 3: Pivot to clinical reality
        ("chat", "Setting ethics aside for a moment — what CRISPR-based therapies are actually in clinical use "
                 "or late-stage trials right now?", 50),

        # Inject second PDF — FDA approval of Casgevy/Lyfgenia
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "CRISPR" / "fda_approval_of_casgevy_and_lyfgenia__a_dual.10.pdf"),
        }, 15),

        # Phase 4: Evaluate the first approved therapies
        ("chat", "Casgevy and Lyfgenia are now FDA-approved for sickle cell disease. "
                 "What can we learn from these first approvals about CRISPR's clinical potential and limitations?", 60),

        # Inject third PDF — gene therapies review
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "CRISPR" / "laurent CRISPR-based gene therapies.pdf"),
        }, 15),

        # Phase 5: Broader therapeutic landscape
        ("chat", "Beyond hemoglobinopathies, which diseases are the most promising targets for CRISPR therapy? "
                 "What technical barriers remain?", 60),

        # Phase 6: Contested question — access and equity
        ("chat", "CRISPR therapies cost over $2 million per treatment. Is this technology going to widen "
                 "health disparities, or can we make it accessible? What would that require?", 60),

        # Phase 7: Future synthesis
        ("chat", "Looking 10 years ahead, what role will CRISPR play in medicine? "
                 "What needs to happen between now and then?", 50),

        ("done", None, 0),
    ],

    "llm-cds": [
        ("wait_ready", None, 8),

        # Phase 1: Start with "what would you do differently"
        ("chat", "If you could redesign clinical decision support systems from scratch using LLMs, "
                 "what would you do differently from current rule-based systems?", 50),

        # Inject first PDF — implementing LLMs in healthcare
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "LLMs in Clinical Decision Support" / "dennstadt implementing LLMs in healthcare.pdf"),
        }, 15),

        # Phase 2: Current landscape — what's actually working?
        ("chat", "Where are LLMs actually being used in clinical decision support today? "
                 "What evidence do we have that they improve outcomes?", 50),

        # Phase 3: Push on limitations
        ("chat", "What are the most dangerous failure modes of LLMs in clinical settings? "
                 "When should we absolutely not trust an LLM's recommendation?", 50),

        # Inject second PDF — safety challenges
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "LLMs in Clinical Decision Support" / "Wang safety challenges of AI in medicine.pdf"),
        }, 15),

        # Phase 4: Safety deep dive
        ("chat", "How do we handle hallucinations, bias, and lack of explainability "
                 "when LLMs are making recommendations that affect patient safety?", 60),

        # Inject third PDF — deterministic framework
        ("ingest", {
            "hat_id": "white-hat",
            "pdf_path": str(HERE / "LLMs in Clinical Decision Support" / "arriola-omontenegro deterministic LLM framework.pdf"),
        }, 15),

        # Phase 5: Deterministic vs probabilistic approaches
        ("chat", "Some researchers argue we need deterministic guardrails around LLM outputs in healthcare. "
                 "Others say that defeats the purpose. Who's right?", 60),

        # Phase 6: Contested question — physician autonomy
        ("chat", "Should an LLM ever be allowed to override a physician's clinical judgment? "
                 "What about cases where the LLM has access to more data than the physician?", 60),

        # Phase 7: Practical synthesis
        ("chat", "What would a responsible deployment framework look like for LLMs in clinical decision support? "
                 "What guardrails are non-negotiable?", 50),

        ("done", None, 0),
    ],
}


async def wait_for_server(timeout: float = 60):
    """Poll until the server is up."""
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(f"{SERVER_URL}/") as resp:
                    if resp.status == 200:
                        print("  Server is up.")
                        return
            except (aiohttp.ClientError, ConnectionRefusedError, OSError):
                pass
            await asyncio.sleep(2)
    raise TimeoutError("Server did not start in time")


async def ingest_pdf(hat_id: str, pdf_path: str):
    """Upload a PDF via the /ingest endpoint."""
    path = Path(pdf_path)
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field("file", open(path, "rb"),
                       filename=path.name,
                       content_type="application/pdf")
        data.add_field("hat_id", hat_id)
        data.add_field("domain", "")
        async with session.post(f"{SERVER_URL}/ingest", data=data) as resp:
            result = await resp.json()
            if result.get("success"):
                print(f"  [Ingest] {hat_id}: {result.get('count', '?')} chunks from {path.name}")
            else:
                print(f"  [Ingest] FAILED: {result.get('error', 'unknown')}")


async def run_session(script: list):
    """Execute the scripted session."""
    turn_count = 0

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            # Background task to read and count messages
            async def reader():
                nonlocal turn_count
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("type") == "message":
                            sender = data.get("sender_name", "?")
                            content = data.get("content", "")[:80]
                            turn_count += 1
                            print(f"  [{turn_count}] {sender}: {content}...")
                        elif data.get("type") == "ingest_complete":
                            print(f"  [Ingest complete] {data.get('hat_name')}: "
                                  f"{data.get('count')} chunks from {data.get('source')}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.ERROR):
                        break

            reader_task = asyncio.create_task(reader())

            # Wait for init
            await asyncio.sleep(3)

            for action, payload, wait in script:
                if action == "wait_ready":
                    print("Waiting for initial activity to settle...")
                    await asyncio.sleep(wait)

                elif action == "chat":
                    print(f"\n>>> USER: {payload}")
                    await ws.send_str(json.dumps({
                        "type": "chat",
                        "content": payload,
                    }))
                    print(f"  Waiting {wait}s for responses...")
                    await asyncio.sleep(wait)

                elif action == "ingest":
                    print(f"\n>>> INJECTING PDF to {payload['hat_id']}")
                    await ingest_pdf(payload["hat_id"], payload["pdf_path"])
                    print(f"  Waiting {wait}s for ingestion reaction...")
                    await asyncio.sleep(wait)

                elif action == "done":
                    print(f"\n=== Session complete. {turn_count} turns captured. ===")
                    print("Check session_logs/ for the full log.")
                    break

            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass


async def run_topic(topic: str):
    """Start server, run one topic session, then shut down."""
    script = SCRIPTS[topic]
    cmd = [sys.executable, "-u", "parlor.py", "--panel", "brainstorming-hats",
           "--backend", _cli_args.backend]
    if _cli_args.model:
        cmd.extend(["--model", _cli_args.model])
    if _cli_args.naive_diffusion:
        cmd.append("--naive-diffusion")
    mode = "naive diffusion" if _cli_args.naive_diffusion else "BEAR-guided diffusion"
    print(f"\n{'='*60}")
    print(f"  Topic: {topic.upper()} | Mode: {mode}")
    print(f"{'='*60}")
    # Optionally wipe the knowledge store for clean evaluation
    if _cli_args.clean:
        import shutil
        kb_path = HERE / "panel_data" / "knowledge"
        if kb_path.exists():
            shutil.rmtree(kb_path)
            print("  Wiped knowledge store for clean run.")

    print(f"Starting BEAR Parlor server...", flush=True)
    server = subprocess.Popen(cmd, cwd=str(HERE))

    try:
        await wait_for_server(timeout=180)
        await asyncio.sleep(2)  # Let embeddings finish loading
        await run_session(script)
    finally:
        print("\nShutting down server...")
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        print("Done.")


async def main():
    if _cli_args.topic == "all":
        for topic in ["dmg", "stroke", "ms", "alzheimers", "epilepsy",
                       "glp1", "crispr", "llm-cds"]:
            await run_topic(topic)
            # Brief pause between sessions
            print("\nPausing 5s before next topic...\n")
            await asyncio.sleep(5)
    else:
        await run_topic(_cli_args.topic)


if __name__ == "__main__":
    asyncio.run(main())
