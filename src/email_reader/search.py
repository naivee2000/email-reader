from __future__ import annotations

import heapq
import json
import math
import re
from dataclasses import dataclass
from typing import Protocol

from email_reader.contracts import Citation
from email_reader.database import Database
from email_reader.image_types import mail_image_sql, visible_mail_file_sql
from email_reader.repository import MailRepository
from email_reader.text import safe_fts_query

_QUESTION_WORDS = frozenset("a an the my me i you your we our of for to from in on at by with and or is are was were be been do does did can could would should will what which who when where how please find show give get tell about email emails mail message messages file files document documents attached attachment attachments it its this that those these need want have has".split())  # noqa: SIM905


def metadata_terms(query: str) -> set[str]:
    return {word for word in re.findall(r"[\w]+", query.casefold())
            if word not in _QUESTION_WORDS and (len(word) >= 2 or word.isdigit())}


def context_terms(value: str) -> set[str]:
    """Topic words plus overlapping CJK characters for page-level selection."""
    terms = metadata_terms(value)
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def salient_bigrams(query: str) -> list[str]:
    """Keep adjacent topic words searchable when an AI query plan misses them."""
    words = [word for word in re.findall(r"[\w]+", query.casefold())
             if word not in _QUESTION_WORDS and len(word) >= 2]
    return list(dict.fromkeys(f"{left} {right}" for left, right in zip(words, words[1:], strict=False)))[:10]

VISIBLE_CHUNK_SQL = """(EXISTS(
    SELECT 1 FROM threads visible_thread
    WHERE visible_thread.id=c.thread_id AND visible_thread.trashed_at IS NULL
) AND (
    (c.source_type='message' AND EXISTS(
      SELECT 1 FROM messages visible_message
      WHERE visible_message.id=c.source_id
        AND (visible_message.in_main_inbox=1 OR visible_message.is_sent=1 OR visible_message.is_draft=1)
        AND visible_message.is_filtered_bulk=0
    )) OR
    (c.source_type='attachment' AND EXISTS(
      SELECT 1 FROM attachments visible_attachment
      JOIN messages visible_parent ON visible_parent.id=visible_attachment.message_id
      WHERE visible_attachment.id=c.source_id
        AND (visible_parent.in_main_inbox=1 OR visible_parent.is_sent=1 OR visible_parent.is_draft=1)
        AND visible_parent.is_filtered_bulk=0
    ))
))"""

# Keep automatic embedding work limited to the focused mailboxes above. A
# deliberate user search has a wider scope: imported promotions, social mail,
# and archived messages may contain the exact fact or file being requested.
# App-trash is still excluded, and account filters still apply at the caller.
SEARCHABLE_CHUNK_SQL = """(EXISTS(
    SELECT 1 FROM threads searchable_thread
    WHERE searchable_thread.id=c.thread_id AND searchable_thread.trashed_at IS NULL
) AND (
    (c.source_type='message' AND EXISTS(
      SELECT 1 FROM messages searchable_message
      WHERE searchable_message.id=c.source_id
    )) OR
    (c.source_type='attachment' AND EXISTS(
      SELECT 1 FROM attachments searchable_attachment
      JOIN messages searchable_parent ON searchable_parent.id=searchable_attachment.message_id
      WHERE searchable_attachment.id=c.source_id
    ))
))"""


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    content: str
    score: float
    citation: Citation


class HybridSearch:
    def __init__(
        self, database: Database, repository: MailRepository, embedder: Embedder | None = None
    ) -> None:
        self.db = database
        self.repository = repository
        self.embedder = embedder

    def index_missing_embeddings(self, batch_size: int = 64) -> int:
        if not self.embedder:
            return 0
        model = getattr(getattr(self.embedder, "settings", None), "embedding_model", None)
        rows = self.db.query(
            f"""SELECT c.id,c.content FROM source_chunks c
               WHERE (c.embedding_json IS NULL OR c.embedding_model IS NOT ?) AND {VISIBLE_CHUNK_SQL}
               ORDER BY c.created_at LIMIT ?""",
            (model, batch_size),
        )
        if not rows:
            return 0
        try:
            vectors = self.embedder.embed([row["content"] for row in rows])
        except (RuntimeError, ConnectionError):
            return 0
        with self.db.transaction() as connection:
            for row, vector in zip(rows, vectors, strict=True):
                connection.execute(
                    "UPDATE source_chunks SET embedding_json=?,embedding_model=? WHERE id=? AND content=?",
                    (json.dumps(vector, separators=(",", ":")), model, row["id"], row["content"]),
                )
        return len(rows)

    def search(
        self, query: str, *, account_id: str | None = None, limit: int = 12,
        semantic: bool = True, semantic_message_id: str | None = None,
    ) -> list[SearchHit]:
        vector = self._query_vector(query) if semantic and not semantic_message_id else None
        with self.db.read_snapshot():
            return self._search_snapshot(query, account_id=account_id, limit=limit,
                                         query_vector=vector, semantic_message_id=semantic_message_id)

    def _search_snapshot(self, query, *, account_id, limit, query_vector, semantic_message_id):
        lexical = self._lexical(
            query, account_id=account_id, limit=max(limit * 3, 20),
            broaden_partial_chinese=True,
        )
        semantic_hits = (
            self._semantic_from_message(
                semantic_message_id, account_id=account_id, limit=max(limit * 3, 20)
            ) if semantic_message_id else (
                self._rank_semantic_vector(query_vector, account_id=account_id, limit=max(limit * 3, 20),
                    model=getattr(getattr(self.embedder, "settings", None), "embedding_model", None))
                if query_vector else []
            )
        )
        scores: dict[str, float] = {}
        content: dict[str, str] = {}
        for rank, row in enumerate(lexical, start=1):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (60 + rank)
            content[row["id"]] = row["content"]
        for rank, row in enumerate(semantic_hits, start=1):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (60 + rank)
            content[row["id"]] = row["content"]
        hits: list[SearchHit] = []
        for chunk_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[
            :limit
        ]:
            citation = self.repository.citation_for_chunk(chunk_id)
            if citation:
                hits.append(SearchHit(chunk_id, content[chunk_id], score, citation))
        return hits

    def conversational_search(
        self, queries: list[str], *, account_id: str | None = None, limit: int = 32
    ) -> list[SearchHit]:
        """Fuse question reformulations, translations, and source-adjacent files."""
        # Any remote embedding call finishes before opening a read transaction.
        vector = self._query_vector(queries[0]) if queries else None
        with self.db.read_snapshot():
            return self._conversational_snapshot(queries, account_id=account_id, limit=limit, query_vector=vector)

    def _conversational_snapshot(self, queries, *, account_id, limit, query_vector):
        scores: dict[str, float] = {}
        contents: dict[str, str] = {}
        lexical_support: dict[str, list[float]] = {}
        candidate_limit = max(80, limit * 3)
        deterministic = [phrase for question in queries[:2] for phrase in salient_bigrams(question)]
        broaden_partial_chinese = len(queries) == 1
        for query in dict.fromkeys(
            item.casefold().strip() for item in [*queries, *deterministic] if item.strip()
        ):
            rows = self._lexical(
                query, account_id=account_id, limit=candidate_limit,
                broaden_partial_chinese=broaden_partial_chinese,
            )
            # Reformulations are alternatives, not independent evidence. A
            # dozen broad phrases must not drown out a selective translation.
            # Use bounded selectivity within this scoped candidate window;
            # this is not a claim about corpus-wide document frequency.
            weight = min(4.0, 1.0 + math.log(candidate_limit / max(1, len(rows))))
            for rank, row in enumerate(rows, 1):
                # A shared Chinese bigram broadens recall but is much weaker
                # than an exact phrase hit; do not let several approximate
                # matches overpower one directly supported source.
                precision = .2 if row.get("approximate") else 1.0
                lexical_support.setdefault(row["id"], []).append(
                    precision * weight / (30 + rank)
                )
                contents[row["id"]] = row["content"]
        for identifier, support in lexical_support.items():
            strongest = sorted(support, reverse=True)[:2]
            scores[identifier] = strongest[0] + (strongest[1] * .25 if len(strongest) > 1 else 0)
        if query_vector:
            for rank, row in enumerate(
                self._rank_semantic_vector(query_vector, account_id=account_id, limit=candidate_limit,
                    model=getattr(getattr(self.embedder, "settings", None), "embedding_model", None)), 1
            ):
                scores[row["id"]] = scores.get(row["id"], 0) + 1 / (30 + rank)
                contents[row["id"]] = row["content"]
        # Body-only FTS cannot match words that exist only in a filename or
        # subject. Also, a full natural-language question is too restrictive for
        # AND-based FTS. Independently match salient literal metadata terms; do
        # not depend on the model remembering every name/number in its phrases.
        peak = max(scores.values(), default=0)
        metadata_queries = (
            queries[:1] if queries and len(metadata_terms(queries[0])) >= 2 else queries[1:2]
        )
        for query in metadata_queries:
            for rank, row in enumerate(self._metadata_matches(query, account_id=account_id), 1):
                scores[row["id"]] = max(scores.get(row["id"], 0), peak + row["coverage"] * .05 + 1 / (100 + rank))
                contents[row["id"]] = row["content"]
        # A question can match the covering email while its PDF has no matching
        # words (or is an unsupported format). Include those exact source files.
        for identifier in sorted(scores, key=scores.get, reverse=True)[:16]:
            rows = self.db.query(
                f"""SELECT c.id,c.content FROM source_chunks origin
                   LEFT JOIN attachments origin_file ON origin.source_type='attachment' AND origin_file.id=origin.source_id
                   JOIN attachments a ON a.message_id=CASE WHEN origin.source_type='message'
                       THEN origin.source_id ELSE origin_file.message_id END
                       AND {visible_mail_file_sql('a')}
                   JOIN source_chunks c ON c.source_type='attachment' AND c.source_id=a.id
                   WHERE origin.id=? AND c.chunk_index=-1 AND NOT {mail_image_sql('a')}
                   ORDER BY CASE WHEN a.mime_type='application/pdf' THEN 0 ELSE 1 END,a.filename LIMIT 12""",
                (identifier,),
            )
            for row in rows:
                scores[row["id"]] = max(scores.get(row["id"], 0), scores[identifier] * 0.95)
                contents[row["id"]] = row["content"]
        hits = []
        per_source: dict[str, int] = {}
        for identifier in sorted(scores, key=scores.get, reverse=True):
            source = self.db.scalar("SELECT source_id FROM source_chunks WHERE id=?", (identifier,))
            if per_source.get(source, 0) >= 2:
                continue
            citation = self.repository.citation_for_chunk(identifier)
            if citation:
                hits.append(
                    SearchHit(identifier, contents[identifier], scores[identifier], citation)
                )
                per_source[source] = per_source.get(source, 0) + 1
            if len(hits) >= limit:
                break
        # Metadata can find a file by name or covering email without finding
        # any text from inside it. Supply all chunks of short top documents, not
        # just their first page. For long files, include boundaries and context
        # around already matched chunks, then query-relevant middle pages and
        # a broad sample, within an eight-chunk per-file budget.
        included = {hit.chunk_id for hit in hits}
        added_files: set[str] = set()
        topic_weights: dict[str, int] = {}
        for position, query in enumerate(queries[:4]):
            for term in context_terms(query):
                topic_weights[term] = max(topic_weights.get(term, 0), 2 if position == 0 else 1)
        for hit in tuple(hits):
            if len(added_files) >= 3:
                break
            row = self.db.connection.execute(
                f"""SELECT c.source_id,c.chunk_index FROM source_chunks c
                   JOIN attachments a ON a.id=c.source_id WHERE c.id=?
                   AND c.source_type='attachment' AND {visible_mail_file_sql('a')}""", (hit.chunk_id,)
            ).fetchone()
            if not row or row["source_id"] in added_files:
                continue
            chunks = self.db.query(
                """SELECT id,chunk_index,content FROM source_chunks WHERE source_type='attachment'
                   AND source_id=? AND chunk_index>=0 ORDER BY chunk_index""",
                (row["source_id"],),
            )
            if not chunks:
                continue
            added_files.add(row["source_id"])
            if len(chunks) <= 8:
                chosen = range(len(chunks))
            else:
                matched = [index for index, chunk in enumerate(chunks) if chunk["id"] in included]
                priority = [0, len(chunks) - 1, *matched]
                for index in matched:
                    priority.extend(neighbor for neighbor in (index - 1, index + 1)
                                    if 0 <= neighbor < len(chunks))
                chosen_indices = list(dict.fromkeys(priority))[:8]
                if len(chosen_indices) < 8 and topic_weights:
                    page_matches = [context_terms(chunk["content"]) & topic_weights.keys()
                                    for chunk in chunks]
                    covered = set().union(*(page_matches[index] for index in chosen_indices))
                    while len(chosen_indices) < 8:
                        novel = [
                            (sum(topic_weights[term] for term in match - covered), -index, index)
                            for index, match in enumerate(page_matches) if index not in chosen_indices
                        ]
                        if not novel:
                            break
                        gain, _, index = max(novel)
                        if gain <= 0:
                            break
                        chosen_indices.append(index)
                        covered.update(page_matches[index])
                for slot in range(1, 8):
                    index = round((len(chunks) - 1) * slot / 8)
                    if len(chosen_indices) < 8 and index not in chosen_indices:
                        chosen_indices.append(index)
                chosen = sorted(chosen_indices)
            for index in chosen:
                identifier = chunks[index]["id"]
                if identifier in included:
                    continue
                citation = self.repository.citation_for_chunk(identifier)
                if citation:
                    content = self.db.scalar("SELECT content FROM source_chunks WHERE id=?", (identifier,))
                    hits.append(SearchHit(identifier, content, hit.score * .8, citation))
                    included.add(identifier)
        return hits

    def _metadata_matches(self, query: str, *, account_id: str | None) -> list[dict]:
        terms = sorted(metadata_terms(query))[:24]
        if len(terms) < 2:
            return []
        # SQL limits the candidate set by overlap, not just recency. The final
        # word-boundary check avoids substring false positives such as car/card.
        field = "lower(CASE WHEN c.source_type='attachment' THEN a.filename ELSE t.subject END)"
        overlap = " + ".join(f"CASE WHEN instr({field},?)>0 THEN 1 ELSE 0 END" for _ in terms)
        scope = "AND c.account_id=?" if account_id else ""
        rows = self.db.query(
            f"""SELECT c.id,{field} AS title,({overlap}) AS overlap_count,t.latest_at
                FROM source_chunks c JOIN threads t ON t.id=c.thread_id
                LEFT JOIN attachments a ON c.source_type='attachment' AND a.id=c.source_id
                WHERE ((c.source_type='message' AND c.chunk_index=0) OR
                       (c.source_type='attachment' AND c.chunk_index=-1
                        AND {visible_mail_file_sql('a')}))
                  AND {SEARCHABLE_CHUNK_SQL} {scope}
                  AND overlap_count>=2
                ORDER BY overlap_count DESC,t.latest_at DESC LIMIT 120""",
            [*terms, *([account_id] if account_id else [])],
        )
        matches = []
        for row in rows:
            title_terms = metadata_terms(row["title"])
            common = set(terms) & title_terms
            coverage = len(common) / max(1, len(terms))
            if len(common) >= 2 and coverage >= .5:
                content = self.db.scalar("SELECT content FROM source_chunks WHERE id=?", (row["id"],))
                matches.append({"id": row["id"], "content": content, "coverage": coverage})
        return sorted(matches, key=lambda item: item["coverage"], reverse=True)[:24]

    def _lexical(
        self, query: str, *, account_id: str | None, limit: int,
        broaden_partial_chinese: bool = False,
    ) -> list[dict[str, str]]:
        fts = safe_fts_query(query)
        if not fts:
            return []
        account_clause = "AND c.account_id=?" if account_id else ""
        scope_clause = f"AND {SEARCHABLE_CHUNK_SQL}"
        # Legacy imports may not have marked signature images as inline. Their
        # copied covering-email metadata must not flood the result window.
        # Keep actual OCR chunks searchable, and allow an explicit filename match.
        scope_clause += f""" AND NOT(c.source_type='attachment' AND c.chunk_index=-1 AND EXISTS(
            SELECT 1 FROM attachments decorative WHERE decorative.id=c.source_id
              AND {mail_image_sql('decorative')}
              AND lower(decorative.filename) NOT LIKE ?
        ))"""
        filename_query = "%" + query.lower().replace("%", "").replace("_", "") + "%"
        parameters: list[object] = [fts]
        if account_id:
            parameters.append(account_id)
        parameters.extend([filename_query, limit])
        try:
            rows = self.db.query(
                f"""SELECT c.id,c.content,bm25(source_fts) AS rank
                    FROM source_fts JOIN source_chunks c ON c.id=source_fts.chunk_id
                    WHERE source_fts MATCH ? {account_clause} {scope_clause}
                    ORDER BY rank LIMIT ?""",
                parameters,
            )
        except Exception:
            like = f"%{query}%"
            rows = self.db.query(
                f"""SELECT id,content,0 AS rank FROM source_chunks c
                    WHERE content LIKE ? {account_clause} {scope_clause} LIMIT ?""",
                [like, *parameters[1:]],
            )
        # Unicode FTS does not segment Chinese sentences into words. A bounded
        # substring fallback supports Chinese translations without exporting text.
        approximate_ids: set[str] = set()
        if re.search(r"[\u3400-\u9fff]", query):
            terms = re.findall(r"[\u3400-\u9fff]+", query)[:6]
            terms = [term for term in terms if len(term) >= 2]
            if terms:
                predicates = " AND ".join("c.content LIKE ?" for _ in terms)
                extra = self.db.query(
                    f"SELECT c.id,c.content,0 AS rank FROM source_chunks c WHERE {predicates} {account_clause} {scope_clause} LIMIT ?",
                    [
                        *(f"%{term}%" for term in terms),
                        *([account_id] if account_id else []),
                        filename_query, limit,
                    ],
                )
                seen = {row["id"] for row in rows}
                rows = [*rows, *(row for row in extra if row["id"] not in seen)][:limit]
                # A translation can be close without appearing verbatim in
                # the mail (汽车保险 versus 汽车保单, for example). If this is the
                # only query formulation, a few exact hits need not cover all
                # documents, so admit lower-ranked overlap candidates. With
                # several reformulations, only broaden a phrase that found
                # nothing; broadening every one degraded source rankings.
                # The answer model must still verify approximate evidence.
                had_exact_rows = bool(rows)
                if not rows or (broaden_partial_chinese and len(rows) < min(limit, 8)):
                    spans = list(dict.fromkeys(
                        term[index:index + 2]
                        for term in terms if len(term) >= 3
                        for index in range(len(term) - 1)
                    ))[:6]
                    if spans:
                        overlap = " + ".join("(instr(c.content,?)>0)" for _ in spans)
                        predicate = " OR ".join("instr(c.content,?)>0" for _ in spans)
                        approximate = self.db.query(
                            f"""SELECT c.id,c.content,({overlap}) AS overlap_count
                                FROM source_chunks c JOIN threads t ON t.id=c.thread_id
                                WHERE ({predicate}) {account_clause} {scope_clause}
                                ORDER BY overlap_count DESC,t.latest_at DESC LIMIT ?""",
                            [*spans, *spans, *([account_id] if account_id else []),
                             filename_query, min(limit, 8) + len(rows)],
                        )
                        seen = {row["id"] for row in rows}
                        new_rows = [row for row in approximate if row["id"] not in seen]
                        if had_exact_rows:
                            approximate_ids = {row["id"] for row in new_rows}
                        rows.extend(new_rows)
                        rows = rows[:limit]
        return [{**dict(row), "approximate": row["id"] in approximate_ids} for row in rows]

    def _semantic(self, query: str, *, account_id: str | None, limit: int) -> list[dict[str, str]]:
        query_vector = self._query_vector(query)
        if not query_vector:
            return []
        model = getattr(getattr(self.embedder, "settings", None), "embedding_model", None)
        return self._rank_semantic_vector(query_vector, account_id=account_id, limit=limit, model=model)

    def _query_vector(self, query: str) -> list[float] | None:
        if not self.embedder:
            return None
        try:
            vectors = self.embedder.embed([query])
            return vectors[0] if vectors else None
        except (RuntimeError, ConnectionError):
            return None

    def _semantic_from_message(
        self, message_id: str, *, account_id: str | None, limit: int
    ) -> list[dict[str, str]]:
        row = self.db.connection.execute(
            "SELECT embedding_json,embedding_model FROM source_chunks "
            "WHERE source_type='message' AND source_id=? AND embedding_json IS NOT NULL "
            "ORDER BY chunk_index LIMIT 1", (message_id,)
        ).fetchone()
        if not row:
            return []
        return self._rank_semantic_vector(
            json.loads(row["embedding_json"]), account_id=account_id,
            limit=limit, model=row["embedding_model"], positive_only=True,
        )

    def _rank_semantic_vector(
        self, query_vector: list[float], *, account_id: str | None,
        limit: int, model: str | None = None, positive_only: bool = False,
    ) -> list[dict[str, str]]:
        clause = f"WHERE c.embedding_json IS NOT NULL AND {SEARCHABLE_CHUNK_SQL}"
        parameters: list[object] = []
        if account_id:
            clause += " AND c.account_id=?"
            parameters.append(account_id)
        if model:
            clause += " AND c.embedding_model=?"
            parameters.append(model)
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if not query_norm:
            return []
        scored: list[tuple[float, str]] = []
        for row in self.db.iter_query(
            f"SELECT c.id,c.embedding_json FROM source_chunks c {clause}", parameters
        ):
            vector = json.loads(row["embedding_json"])
            if len(query_vector) != len(vector):
                continue
            vector_norm = math.sqrt(sum(value * value for value in vector))
            score = (
                sum(left * right for left, right in zip(query_vector, vector, strict=True))
                / (query_norm * vector_norm)
                if vector_norm else 0.0
            )
            if not positive_only or score > 0:
                scored.append((score, row["id"]))
        top_ids = [
            identifier for _, identifier in
            heapq.nlargest(limit, scored, key=lambda item: item[0])
        ]
        if not top_ids:
            return []
        placeholders = ",".join("?" for _ in top_ids)
        contents = {
            row["id"]: row["content"]
            for row in self.db.query(
                f"SELECT id,content FROM source_chunks WHERE id IN ({placeholders})", top_ids
            )
        }
        return [
            {"id": identifier, "content": contents[identifier]}
            for identifier in top_ids if identifier in contents
        ]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0
