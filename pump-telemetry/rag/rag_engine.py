"""Retrieve the pump manual with FAISS and select a cited checklist with Groq."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from groq import APIError, Groq
from langchain_community.vectorstores.faiss import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer

from model.pump_pipeline import FEATURES

MANUAL = Path(__file__).with_name("pump_manual.pdf")
DEFAULT_MODEL = "openai/gpt-oss-20b"
QUERY_TERMS = {
    "Bearing wear": "bearing wear bearing condition vibration motor current lubrication alignment",
    "Overheating": "overheating temperature rise motor current cooling ventilation electrical load",
    "Cavitation": "cavitation low suction pressure high vibration impeller inlet restriction",
    "Unclassified": "general triage and safe handoff verify signal quality operating baseline",
}


@dataclass(frozen=True)
class Step:
    id: str
    page: int
    text: str


class ManualEmbeddings(Embeddings):
    """Use local TF-IDF vectors so indexing requires no external embedding service."""

    def __init__(self, pages: list[str]):
        self.vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english").fit(pages)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class RagEngine:
    def __init__(self, manual: Path = MANUAL):
        self.model = os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
        key = os.environ.get("GROQ_API_KEY", "").strip()
        self.client = None
        self.fallback_reason = "key_missing"
        if key:
            try:
                self.client = Groq(api_key=key, timeout=12.0, max_retries=0)
            except (ImportError, ValueError):
                self.fallback_reason = "provider_unavailable"
        self.steps: dict[int, list[Step]] = {}
        self.titles: dict[int, str] = {}
        documents = []
        for page, source in enumerate(PdfReader(manual).pages, start=1):
            text = source.extract_text().strip()
            if "\nChecklist\n" not in text:
                continue
            title = text.splitlines()[0].strip()
            section = text.split("\nChecklist\n", 1)[1].split("\nEscalation record\n", 1)[0]
            items = re.findall(
                r"^[ \t]*(\d+)\.[ \t]*(.*?)(?=^[ \t]*\d+\.[ \t]*|\Z)",
                section, re.M | re.S,
            )
            self.steps[page] = [
                Step(f"p{page}s{number}", page, " ".join(content.split()))
                for number, content in items
            ]
            self.titles[page] = title
            if len(self.steps[page]) != 4:
                raise ValueError(f"Expected four manual steps on page {page}.")
            documents.append(Document(page_content=text, metadata={"page": page}))
        if set(self.steps) != {2, 3, 4, 5}:
            raise ValueError("The training manual needs a general procedure and three fault procedures.")
        self.index = FAISS.from_documents(
            documents, ManualEmbeddings([doc.page_content for doc in documents])
        )

    def _groq_plan(self, reading: dict, allowed: list[Step], primary: int) -> list[Step]:
        observation = {
            "suspected_fault": reading["suspected_fault"],
            "sensor_readings": {feature: reading[feature] for feature in FEATURES},
            "fault_score": reading["fault_score"],
            "primary_page": primary,
            "manual_steps": [asdict(step) for step in allowed],
        }
        completion = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            temperature=0,
            max_completion_tokens=768,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Select 2 to 5 relevant checklist IDs for a simulated pump alert. "
                        "Return JSON with exactly one field named step_ids, an array of strings. "
                        "Use only the supplied IDs. Include the primary page's first verification "
                        "step and at least one more step from that page. General triage may help. "
                        "The UI supplies the energy-control step separately."
                    ),
                },
                {"role": "user", "content": json.dumps(observation)},
            ],
        )
        payload = json.loads(completion.choices[0].message.content)
        ids = payload.get("step_ids")
        if not isinstance(ids, list) or not 2 <= len(ids) <= 5:
            raise ValueError("Invalid step count")
        if any(not isinstance(item, str) for item in ids):
            raise ValueError("Invalid step ID")
        selected = set(ids)
        allowed_ids = {step.id for step in allowed}
        primary_ids = {step.id for step in self.steps[primary]}
        if (
            len(selected) != len(ids)
            or not selected <= allowed_ids
            or self.steps[primary][0].id not in selected
            or len(selected & primary_ids) < 2
        ):
            raise ValueError("Selection is outside the supplied procedure")
        return [step for step in allowed if step.id in selected]

    def advise(self, reading: dict) -> dict:
        suspected = reading["suspected_fault"]
        query = QUERY_TERMS.get(suspected, QUERY_TERMS["Unclassified"])
        match = self.index.similarity_search(query, k=1)[0]
        primary = int(match.metadata["page"])
        safety = self.steps[2][-1]
        primary_steps = [step for step in self.steps[primary] if step != safety]
        allowed = [*self.steps[2][:-1], *primary_steps] if primary != 2 else primary_steps
        steps = primary_steps
        mode = "manual"
        reason = self.fallback_reason
        if self.client is not None:
            try:
                steps = self._groq_plan(reading, allowed, primary)
                mode, reason = "groq", None
            except APIError:
                reason = "provider_unavailable"
            except (AttributeError, IndexError, TypeError, ValueError):
                reason = "invalid_provider_response"
        return {
            "title": self.titles[primary],
            "mode": mode,
            "model": self.model if mode == "groq" else None,
            "reason": reason,
            "safety": asdict(safety),
            "steps": [asdict(step) for step in steps],
        }
