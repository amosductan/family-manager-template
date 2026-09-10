# How it avoids small mistakes

A family app loses its users one small error at a time. Each rule below exists because the
failure it prevents actually happened while this app was in daily use. Names are changed.

## A closure arrived four times

Labor Day came in from the preschool's email, the district feed, a calendar invite and a second
calendar, each worded differently. The home page showed four rows.

**Rule:** duplicates collapse on read. Same day, same kid, and one title equal to or a word-subset
of the other (at least two words, so "Swim" can't swallow "Swim party"). The richest copy wins:
timed over all-day, more detail over less. Storage is untouched, so nothing is lost.

## A pool closing became 14 days off school

A rec-center newsletter said the pool would close for maintenance for three weeks. The first line
of that paragraph was stored as a closure, and the days-off board showed 14 phantom "school closed"
days, including both kids' first day of school.

**Rule:** a first line longer than a title's length cap is prose, not a title, and is refused.
An amenity closing is not a school closing.

## The app read its own events back

The days-off board creates calendar events like "Sam off: Leo (school closed)". The nightly
calendar read picked them up, typed them as closures because the title says "closed", and, being
longer, they replaced the real closure as the stated reason for the day.

**Rule:** events the app created carry a private marker and a known title shape, and are dropped
at read time.

## A summary kept inserting the same party

A group-message summary reworded a birthday party every run ("Theo's party", "Party for Theo"),
and the table's uniqueness key couldn't see that. Five rows by the end of the day.

**Rule:** anything mirrored from a summary carries an identity key built from where it came from,
the date, and the core of the title. A re-run updates one row.

## A half day isn't a day off

An early dismissal was once booked as a parent's day off. Twelve calendar events had to be
deleted by hand.

**Rule:** an early dismissal is a pickup, never a day off, and the server refuses to create a
day-off event for one. A day-off event also refuses to be created while nobody's covering it.

## A login expired and the feed went quiet

A calendar token expired and the nightly job failed for 13 nights. The board kept rendering
yesterday's events and looked healthy.

**Rule:** every nightly step reports its own result, and a failed step sends one email when the
state changes. A stale login is a failed step, never a quiet feed.

## A failed fetch looked like a quiet day

**Rule:** every fetch has three outcomes: found, the source answered with nothing, or we couldn't
look. Only the first is cached. A timeout is never stored as an empty answer.

## The assistant made up a change

**Rule:** the assistant can propose from a fixed menu of changes (add a payment, tick a checklist
item, add an event). Each proposal is validated, shown as a card, and applied only when a parent
presses Apply. Every applied change records who and what it replaced.
