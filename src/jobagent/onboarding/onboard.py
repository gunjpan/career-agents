import time
from collections import Counter
from collections.abc import Callable

import httpx

from jobagent.adapters.ats.base import ATSAdapter
from jobagent.agents.prompts import Prompt
from jobagent.agents.runner import call_agent
from jobagent.llm.base import LLMError, LLMProvider, check_provider_policy
from jobagent.models.company import Company
from jobagent.models.onboarding import CareersGuess, OnboardingConfig, OnboardingResult
from jobagent.models.scoring import Pricing
from jobagent.models.tailoring import AgentModel
from jobagent.onboarding.detect import (
    Signature,
    embedded_job_ids,
    fetch_page,
    find_signatures,
    host_matches,
    names_match,
    probe_slugs,
    slug_candidates,
)

ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com")
MAX_BOARDS_TO_VALIDATE = 3


def _needs_review(reason: str, *, ats: str = "", board_id: str = "", evidence=(), found: int = 0):
    return OnboardingResult(status="needs_review", ats=ats, board_id=board_id, reason=reason, evidence=list(evidence), postings_found=found)  # fmt: skip


class Onboarder:
    """Company name -> job platform and board id. The model only suggests where to look; code
    fetches the pages, recognises the platform, and validates by reading real postings. When
    anything is unclear the answer is needs_review with a reason, never a guess."""

    def __init__(
        self,
        provider: LLMProvider,
        config: OnboardingConfig,
        prompt: Prompt,
        adapters: dict[str, ATSAdapter],
        http: httpx.Client,
        pricing: Pricing | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        check_provider_policy(provider, public_data=True)  # only public company names are sent
        self.provider, self.config, self.prompt = provider, config, prompt
        self.adapters, self.http, self.sleep = adapters, http, sleep
        self.pricing = pricing or Pricing(input=0, output=0, cache_read=0, cache_write=0)
        self.agent = AgentModel(model=config.model, prompt=config.prompt, max_tokens=config.max_tokens, temperature=0.0, effort=None)  # fmt: skip
        self.tokens: Counter[str] = Counter()
        self.cost_usd = 0.0

    # --- the model's part: a hint ----------------------------------------------------------

    def guess(self, name: str) -> CareersGuess:
        run = call_agent(
            self.provider, self.agent, self.prompt, self.pricing,
            prefix=self.prompt.text, message=f"<company>{name}</company>", schema=CareersGuess,
        )  # fmt: skip
        self.tokens["input"] += run.input_tokens
        self.tokens["output"] += run.output_tokens
        self.cost_usd += run.cost_usd
        return run.output

    # --- code's part: verification ---------------------------------------------------------

    def validate(self, name: str, ats: str, board_id: str) -> tuple[int, str]:
        """(postings found, error text). Fetches for real through the same adapter the pipeline uses."""
        adapter = self.adapters.get(ats)
        if adapter is None:
            return 0, f"no adapter for {ats}"
        company = Company(name=name, ats=ats, board_id=board_id, status="active")
        try:
            probe = getattr(adapter, "probe", None)
            return (probe(company) if probe else len(adapter.fetch(company))), ""
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return 0, f"{type(e).__name__}: {str(e)[:100]}"

    def _signatures(self, guess: CareersGuess):
        """Platform links on the model's suggested pages.

        Returns (signatures, pages read, pages skipped, job ids embedded by platform)."""
        counts: Counter[tuple[str, str]] = Counter()
        read: list[str] = []
        skipped: list[str] = []
        embedded: dict[str, set[str]] = {}
        urls = [u for u in guess.careers_urls if u.startswith("http")][
            : self.config.max_candidate_urls
        ]
        for sig in find_signatures(" ".join(urls)):  # the suggested URL may itself be a job board
            counts[(sig.ats, sig.board_id)] += sig.count

        def scan(url: str) -> list[str]:
            self.sleep(self.config.request_delay_s)
            page = fetch_page(self.http, url)
            if page is None:
                skipped.append(f"{url} (unreachable)")
                return []
            final, html = page
            company_hosted = not any(h in final for h in ATS_HOSTS)
            if (
                company_hosted
                and guess.official_domain
                and not host_matches(final, guess.official_domain)
            ):
                skipped.append(
                    f"{final} (not on {guess.official_domain})"
                )  # a lookalike or redirect elsewhere
                return []
            read.append(final)
            sigs = find_signatures(html)
            for sig in sigs:
                counts[(sig.ats, sig.board_id)] += sig.count
            for ats, ids in embedded_job_ids(html).items():
                embedded.setdefault(ats, set()).update(ids)
            if any(s.ats == "phenom" for s in sigs) and not any(s.supported for s in sigs):
                return [
                    final.rstrip("/") + "/search-results"
                ]  # Phenom lists its jobs (and Workday link) here
            return []

        for url in urls:
            for follow in scan(url):
                scan(follow)
        sigs = [Signature(a, b, n) for (a, b), n in counts.items()]
        return (
            sorted(sigs, key=lambda s: (not s.supported, -s.count, s.ats)),
            read,
            skipped,
            embedded,
        )

    def onboard(self, company: Company) -> OnboardingResult:
        # 1. The user already told us the platform: no model needed, just check it works.
        if company.ats and company.board_id:
            n, err = self.validate(company.name, company.ats, company.board_id)
            if n > 0:
                return OnboardingResult(status="active", ats=company.ats, board_id=company.board_id, reason=f"given in the Companies tab; {n} postings found", evidence=["ats and board_id given by hand"], postings_found=n)  # fmt: skip
            return _needs_review(f"given {company.ats}/{company.board_id} but it returned no postings. {err}".strip(), ats=company.ats, board_id=company.board_id)  # fmt: skip

        # 2. Ask the model where to look.
        try:
            guess = self.guess(company.name)
        except LLMError as e:
            return OnboardingResult(status="pending", reason=f"model unavailable, will retry: {e}")
        if not guess.known:
            return _needs_review(
                "the model does not recognise this company; set ats and board_id by hand"
            )

        # 3. Read the suggested pages and look for a job platform.
        sigs, read, skipped, embedded = self._signatures(guess)
        supported = [s for s in sigs if s.supported]
        if supported:
            valid: list[tuple[Signature, int]] = []
            last_err = ""
            for sig in supported[:MAX_BOARDS_TO_VALIDATE]:
                n, err = self.validate(company.name, sig.ats, sig.board_id)
                last_err = err or last_err
                if n > 0:
                    valid.append((sig, n))
            evidence = [
                f"{s.ats}/{s.board_id} linked {s.count}x on {', '.join(read) or 'suggested URLs'}"
                for s, _ in valid
            ]
            if len(valid) == 1:
                sig, n = valid[0]
                return OnboardingResult(status="active", ats=sig.ats, board_id=sig.board_id, reason=f"{sig.ats} board linked from the company's careers page; {n} postings found", evidence=evidence, postings_found=n)  # fmt: skip
            tenants = {
                (s.ats, s.board_id.rpartition("/")[0]) for s, _ in valid
            }  # e.g. ("workday", "wd3/cibc")
            if len(valid) > 1 and len(tenants) == 1 and valid[0][0].ats == "workday":
                # One company, several sites (e.g. main and campus): the largest is the main board.
                valid.sort(key=lambda sn: -sn[1])
                (sig, n), others = valid[0], valid[1:]
                alt = ", ".join(f"{s.board_id.rpartition('/')[2]} ({m})" for s, m in others)
                return OnboardingResult(status="active", ats=sig.ats, board_id=sig.board_id, reason=f"largest of {len(valid)} {sig.ats} sites on the same tenant ({n} postings); other sites: {alt}", evidence=evidence, postings_found=n)  # fmt: skip
            if len(valid) > 1:
                top = valid[0][0]
                names = ", ".join(f"{s.ats}/{s.board_id}" for s, _ in valid)
                return _needs_review(f"several working boards found ({names}); keep the right one", ats=top.ats, board_id=top.board_id, evidence=evidence, found=valid[0][1])  # fmt: skip
            top = supported[0]
            return _needs_review(f"found {top.ats}/{top.board_id} on the careers page but it returned no postings. {last_err}".strip(), ats=top.ats, board_id=top.board_id)  # fmt: skip

        others = [s for s in sigs if not s.supported and s.ats != "phenom"]
        if others:
            top = others[0]
            return _needs_review(f"uses {top.ats} ({top.board_id}); no adapter for it yet", ats=top.ats, board_id=top.board_id)  # fmt: skip

        # 4. No link found. A board with the company's name may still exist, but that is only a lead.
        slugs = slug_candidates(company.name, guess.board_id_hint, guess.official_domain)
        hits = probe_slugs(self.http, slugs)
        confirmed = [h for h in hits if h.job_ids & embedded.get(h.ats, set())]
        named = [h for h in hits if h.board_name and names_match(h.board_name, company.name)]
        if not confirmed and len(named) == 1:  # Greenhouse states whose board it is
            h = named[0]
            n, _ = self.validate(company.name, h.ats, h.slug)
            return OnboardingResult(status="active", ats=h.ats, board_id=h.slug, reason=f"greenhouse board is named '{h.board_name}', matching the company; {n} postings found", evidence=[f"greenhouse/{h.slug} named '{h.board_name}'"], postings_found=n)  # fmt: skip
        if len(confirmed) == 1:  # the careers page links to jobs that exist on exactly this board
            h = confirmed[0]
            n, _ = self.validate(company.name, h.ats, h.slug)
            shared = len(h.job_ids & embedded[h.ats])
            return OnboardingResult(status="active", ats=h.ats, board_id=h.slug, reason=f"{h.ats} board confirmed: {shared} job ids on the careers page match its postings; {n} postings found", evidence=[f"{h.ats}/{h.slug} job ids match the careers page"], postings_found=n)  # fmt: skip
        if len(hits) == 1:
            h = hits[0]
            n, _ = self.validate(company.name, h.ats, h.slug)
            return _needs_review(f"a {h.ats} board named '{h.slug}' has jobs, but no careers page links to it; confirm it is this company", ats=h.ats, board_id=h.slug, evidence=[f"{h.ats}/{h.slug} found by name"], found=n)  # fmt: skip
        if hits:
            return _needs_review("several boards match the company name: " + ", ".join(f"{h.ats}/{h.slug}" for h in hits) + "; pick the right one")  # fmt: skip
        where = ", ".join(read) if read else "no reachable official page"
        extra = f" (skipped: {'; '.join(skipped)})" if skipped else ""
        phenom = " It looks like a Phenom site whose job platform could not be read." if any(s.ats == "phenom" for s in sigs) else ""  # fmt: skip
        return _needs_review(f"no supported job platform found on {where}{extra}.{phenom}".strip())
