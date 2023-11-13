import logging
import re
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from github.NamedUser import NamedUser
from github.PullRequest import PullRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeNote:
    """Describes an atomic change in the notes."""

    content: str
    reference_name: str
    reference_url: str
    labels: tuple[str, ...]
    timestamp: datetime

    @classmethod
    def from_pull_requests(
        cls,
        pull_requests: set[PullRequest],
        *,
        pr_summary_regex: re.Pattern,
    ) -> "set[ChangeNote]":
        """Create a set of notes from pull requests.

        Create one or more notes describing an atomic change from each given
        pull request.

        `pr_summary_regex` is used to detect one or multiple  notes in a pull
        request description that will be used instead of the pull request title
        if present. This uncouples pull requests and notes somewhat. While
        ideally, a pull request introduces a change that would be described in
        a single note, this is often not the case.
        """
        notes = set()
        for pr in pull_requests:
            if not pr.body or not (
                matches := tuple(pr_summary_regex.finditer(pr.body))
            ):
                logger.debug("falling back to title for %s", pr.html_url)
                matches = ({"summary": pr.title, "label": None},)
            assert len(matches) >= 1
            for match in matches:
                summary = match["summary"]
                if match["label"] is not None:
                    labels = (match["label"],)
                else:
                    labels = tuple(label.name for label in pr.labels)
                notes.add(
                    cls(
                        content=summary.strip(),
                        reference_name=f"#{pr.number}",
                        reference_url=pr.html_url,
                        labels=labels,
                        timestamp=pr.merged_at,
                    )
                )
        return notes


@dataclass(frozen=True, kw_only=True)
class MdFormatter:
    """Format release notes in Markdown from PRs, authors and reviewers."""

    repo_name: str
    change_notes: set[ChangeNote]
    authors: set[NamedUser]
    reviewers: set[NamedUser]

    version: str
    title_template: str
    intro_template: str
    outro_template: str

    # Associate regexes matching PR labels to a section titles in the release notes
    label_section_map: dict[str, str]

    ignored_user_logins: tuple[str, ...]

    def __str__(self) -> str:
        """Return complete release notes document as a string."""
        return self.document

    def __iter__(self) -> Iterable[str]:
        """Iterate the release notes document line-wise."""
        return self.iter_lines()

    @property
    def document(self) -> str:
        """Return complete release notes document as a string."""
        return "".join(self.iter_lines())

    def iter_lines(self) -> Iterable[str]:
        """Iterate the release notes document line-wise."""
        title = self.title_template.format(
            repo_name=self.repo_name, version=self.version
        )
        yield from self._format_section_title(title, level=1)
        yield "\n"
        yield from self._format_intro()
        for title, notes in self._notes_by_section.items():
            yield from self._format_change_section(title, notes)
        yield from self._format_contributor_section(self.authors, self.reviewers)
        yield from self._format_outro()

    @property
    def _notes_by_section(self) -> OrderedDict[str, set[ChangeNote]]:
        """Map change notes to section titles."""
        label_section_map = {
            re.compile(pattern, flags=re.IGNORECASE): section_name
            for pattern, section_name in self.label_section_map.items()
        }

        notes_by_section = OrderedDict()
        for _, section_name in self.label_section_map.items():
            notes_by_section[section_name] = set()
        notes_by_section["Other"] = set()

        for note in self.change_notes:
            matching_sections = [
                section_name
                for regex, section_name in label_section_map.items()
                if any(regex.match(label) for label in note.labels)
            ]
            for section_name in matching_sections:
                notes_by_section[section_name].add(note)
            if not matching_sections:
                logger.warning(
                    "%s without matching label, sorting into section 'Other'",
                    note.reference_url,
                )
                notes_by_section["Other"].add(note)
        return notes_by_section

    def _sanitize_text(self, text: str) -> str:
        """Remove newlines and strip whitespace."""
        text = text.strip()
        text = text.replace("\r\n", " ")
        text = text.replace("\n", " ")
        return text

    def _format_link(self, name: str, target: str) -> str:
        return f"[{name}]({target})"

    def _format_section_title(self, title: str, *, level: int) -> Iterable[str]:
        yield f"{'#' * level} {title}\n"

    def _format_change_note(self, note: ChangeNote) -> Iterable[str]:
        """Format a note about an atomic change."""
        link = self._format_link(note.reference_name, note.reference_url)
        summary = self._sanitize_text(note.content).rstrip(".")
        summary = f"- {summary} ({link}).\n"
        yield summary

    def _format_change_section(
        self, title: str, notes: set[ChangeNote]
    ) -> Iterable[str]:
        """Format a section title and list its items sorted by merge date."""
        if notes:
            yield from self._format_section_title(title, level=2)
            yield "\n"

            for item in sorted(notes, key=lambda note: note.timestamp):
                yield from self._format_change_note(item)
            yield "\n"

    def _format_user_line(self, user: NamedUser) -> str:
        line = f"@{user.login}"
        line = self._format_link(line, user.html_url)
        if user.name:
            line = f"{user.name} ({line})"
        return f"- {line}\n"

    def _format_contributor_section(
        self,
        authors: set[NamedUser],
        reviewers: set[NamedUser],
    ) -> Iterable[str]:
        """Format contributor section and list users sorted by login handle."""
        authors = {u for u in authors if u.login not in self.ignored_user_logins}
        reviewers = {u for u in reviewers if u.login not in self.ignored_user_logins}

        yield from self._format_section_title("Contributors", level=2)
        yield "\n"

        yield f"{len(authors)} authors added to this release (alphabetically):\n"
        yield "\n"
        author_lines = map(self._format_user_line, authors)
        yield from sorted(author_lines, key=lambda s: s.lower())
        yield "\n"

        yield f"{len(reviewers)} reviewers added to this release (alphabetically):\n"
        yield "\n"
        reviewers_lines = map(self._format_user_line, reviewers)
        yield from sorted(reviewers_lines, key=lambda s: s.lower())
        yield "\n"

    def _format_intro(self):
        intro = self.intro_template.format(
            repo_name=self.repo_name, version=self.version
        )
        # Make sure to return exactly one line at a time
        yield from (f"{line}\n" for line in intro.split("\n"))

    def _format_outro(self) -> Iterable[str]:
        outro = self.outro_template.format(
            repo_name=self.repo_name, version=self.version
        )
        # Make sure to return exactly one line at a time
        yield from (f"{line}\n" for line in outro.split("\n"))


class RstFormatter(MdFormatter):
    """Format release notes in reStructuredText from PRs, authors and reviewers."""

    def _sanitize_text(self, text) -> str:
        """Remove newlines, strip whitespace and convert literals to rST syntax."""
        text = super()._sanitize_text(text)
        text = text.replace("`", "``")
        return text

    def _format_link(self, name: str, target: str) -> str:
        return f"`{name} <{target}>`_"

    def _format_section_title(self, title: str, *, level: int) -> Iterable[str]:
        yield title + "\n"
        underline = {1: "=", 2: "-", 3: "~"}
        yield underline[level] * len(title) + "\n"
