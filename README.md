# shelf

A self-hosted web library for documents: reference metadata, a PDF reader, and an OCR pipeline.

I wanted a reference manager I could run on my own hardware, that kept the PDFs searchable, so I wrote one. It works for what I use it for. It is not a product, and it has one deployment behind it — expect rough edges, missing conveniences, and an API that will change without much ceremony.

**Status:** early development. The API and data model are not stable.

![shelf's landing page: a single search box over every space you can read, with links to the library and settings](docs/screenshots/landing.png)

This README is about *using* shelf — the web interface and the `shelf` CLI. Running an instance, configuring it, and working on the code are in [DEVELOPERS.md](./DEVELOPERS.md).

## What's in it

- Items with type-specific metadata, nested collections, tags, notes, creators.
- An in-browser PDF reader with annotations, a generated outline, and pinch-zoom. Highlights are linkable — **Copy link** on one gives a URL that opens the document at that passage and rings it.
- Search over item metadata and the text extracted from each PDF page, with fuzzy title matching. The landing page searches every space you can read at once — your own shelf, spaces shared with you, and the ones those subscribe to — listing each document once no matter how many of those reach it, with a filter for taking a noisy library back out.
- Background OCR and text extraction, so scanned PDFs become searchable too. Originals are kept; OCR output becomes a new version you can switch between.
- Upload Word, PowerPoint, spreadsheet or image files and they are converted to PDF in the background — read, searched, OCR'd and annotated like any other PDF, while downloading still gives back the file you put in.
- Export to BibTeX, CSL-JSON, and Zotero RDF — the last optionally bundled as a ZIP with the files, in the layout Zotero's own translator produces.
- Bulk select, bulk add-to-collection, and a ZIP download that serves each document's current best version.
- Download a selection, a collection or a whole space as a re-importable archive: the original PDFs in their collection folders, plus an `index.json` with each document's metadata, tags and collections, and optionally your own notes and highlights and standard revision links. Import it into any space on any instance; re-importing is idempotent. The format is versioned and specified in [docs/archive-format.md](docs/archive-format.md).
- Spaces as the unit of ownership and sharing, with read-only inheritance between them.
- OIDC login against any compliant provider, plus scoped API tokens for scripts.
- A command-line client, `shelf`, for searching, browsing and bulk metadata from a terminal.

## The `shelf` CLI

[`cli/`](cli/README.md) is a command-line client for a shelf instance,
installable on its own with [pixi](https://pixi.sh):

```sh
pixi global install shelf-cli \
  --git https://github.com/Krande/shelf.git --subdirectory cli --tag v0.18.0
```

The tag is the shelf release the client belongs to; it is kept at the latest
one. pixi records that tag in its global manifest, so `pixi global update`
re-installs the *same* release rather than moving to a newer one. To upgrade,
run the command above again with the new tag. Point it at an instance with a token minted under Settings → API tokens
(see [API tokens](#api-tokens)):

```toml
# shelf.toml
[instance]
base_url = "https://shelf.example.com"
space = "standards"
```

```sh
export SHELF_API_TOKEN=shelf_…
```

`shelf whoami` checks the token works and lists the spaces it reaches:

![shelf whoami: the token is accepted, and reaches a personal shelf and a Standards space, both read-write](docs/screenshots/cli-whoami.svg)

The terminal figures here are the real commands run against a demo library by
`pixi run cli-stories` (see [DEVELOPERS.md](./DEVELOPERS.md#readme-screenshots)).

### Searching from the terminal

`shelf search` finds what the landing page finds — titles, creators, abstracts
and PDF body text — as JSON, with `--hits N` adding each document's first N
matching pages:

![shelf search "load case" --scope fulltext --hits 1: a report, its metadata, and the page the phrase was found on](docs/screenshots/cli-search.svg)

`shelf browse` is the same search as a terminal UI. Each document is listed
with the pages it matched; `enter` opens the highlighted page in the shelf
reader, `o` opens the PDF locally at that page, and `c` narrows the search to
a space or collection:

![shelf browse "load case": spaces in a sidebar, matching documents with their page hits, and a preview of the highlighted one](docs/screenshots/cli-browse.svg)

### Setting metadata

`shelf items set` changes the fields you name and leaves the rest of the
document's metadata alone:

![shelf items set <id> --set numberOfPages=3 --set language=en, printing the updated item](docs/screenshots/cli-items-set.svg)

### Document profiles

**Document profiles** are the reason the CLI exists: metadata kept in JSON
files, somewhere other than an instance, pushed in whenever one is ready.
Pushing twice updates rather than duplicates, matching on the document's own
identity where it has one and on its file's SHA-256 where it doesn't.
`--dry-run` says what a push would do first:

![shelf profiles push ./profiles/ --dry-run: one profile would update an existing edition, the other would create a new standard](docs/screenshots/cli-profiles-push.svg)

The [CLI README](cli/README.md) covers configuration, all the browse keys,
opening a PDF at a page, and how profiles find their document.

## The web interface

The screenshots below are generated from a demo library by
`pixi run ui-stories` (see [DEVELOPERS.md](./DEVELOPERS.md#readme-screenshots)),
so they show the interface as it currently is.

### Searching

The landing page searches everything you can read at once — titles, creators,
abstracts, and the text of every PDF page — and says which of those each hit
came from. A PDF-body hit unfolds into the pages that matched, each a link into
the reader at that page.

![Searching for "load case" across every space: a title hit, an abstract hit, and two standards matched in their PDF text](docs/screenshots/search.png)

### The library

Items in a space, filed into nested collections and tagged. Picking one opens
its details beside the list: the metadata for its type, attachments, and notes.

![The library with a journal article selected: its metadata, tags, collection, attachment and a shared note](docs/screenshots/library.png)

### Linking into a document

The reader reads three query params:

| Param | Points at |
|---|---|
| `?page=N` | a page. Written back as you scroll, so reload and back/forward restore your position |
| `?annotation=<id>` | a highlight — its page, plus a ring on the passage itself |
| `?find=term` | opens the find toolbar pre-filled. A search, not an address: it lands on the first textual match, or nowhere if OCR mangled the word |

`?annotation=` is the precise one. Annotations carry rects in PDF
user-space, which survive zoom, re-render and DPI differences, so the link
resolves to the same passage for everyone who can open the space. Get one from
**Copy link** in the highlights panel.

![The reader opened from a highlight link: the passage ringed on page 2, the highlights panel alongside](docs/screenshots/reader.png)

#### Linking by file, not by id

`/reader/<id>` names this instance's id for an attachment, which nothing
outside the library knows. `/open` names the document instead, so a link can
be built by anything that holds the PDF, and works on every shelf instance
that has the file:

```
https://shelf.example.com/open?sha256=<hex digest of the file>&page=57&find=Table%203.1
```

It looks the file up among the spaces you can read and replaces itself with
the reader, passing `page` and `find` along. A standard can be named by its
edition as well — `&body=…&designation=…&label=…`, matched the way filing a
revision matches — which is tried when the hash finds nothing, since two
downloads of one standard rarely hash the same. When neither matches, the page
says the document isn't in your library; a signed-out visitor signs in first
and comes back to the same link.

The hash is the one of the file as uploaded, recorded when the upload goes
through the API or when the worker first extracts its text, so a file is
findable this way once its text has been extracted.

### Document details while reading

The **ⓘ** button at the top right of the reader opens the document's details
in a drawer — the same panel the library shows, so you can check a
reference, edit the metadata, add a tag or a note, or step to another edition
of a standard without leaving the page. Tags, collections and other editions
link back into the library of the space the document lives in.

![The reader with the document-details drawer open on the right: metadata, tags, collection, attachment and notes](docs/screenshots/reader-info.png)

Nothing smaller than that is addressable: shelf never extracts tables, figures
or equations as objects, so there's no identifier to put in a URL for them.
`?page=N&find=Table%203.1` is the honest workaround and it is a guess, not an
anchor.

### Switching between accounts

If you have more than one identity — two work accounts at different tenants, say
— you can attach them to the same browser session and flip between them without
logging out, from the menu in the header or **Switch user** under Settings →
Account. "Add account" asks the provider for its account picker, so you
actually get a choice of which identity to sign in as. Each account keeps its
own spaces and library; switching changes who you are and nothing else.

The linked set lives in the session cookie, so it only ever contains accounts
that completed a login in this browser, and it lasts as long as the session.
Signing out clears all of them at once; unlink one from Settings to drop just
that one.

### Sharing a space

Spaces are the unit of sharing. Each one has a creator, who is always its owner,
plus any number of members at one of two levels:

| Role | Can |
|---|---|
| `viewer` | read items, attachments, notes and tags; search; export |
| `editor` | all of the above, plus create, edit and delete content |
| `owner` | all of the above, plus manage who has access |

Owner belongs to the creator and isn't assignable — there's no second owner, and
no membership row to delete that would lock the creator out of their own space.
An editor can fill a space but can't widen access to it, so "who else can see
this" stays the owner's decision.

Everyone gets a personal space at first login. **Admins can create additional
shared spaces** from Settings → Spaces — the creator owns it and picks who else
is in it. Creation is admin-gated because spaces are cheap to make and awkward
to clean up; making a space still grants nothing over spaces other people own.

A space's name and slug are edited in its **Profile** (see [Profiles](#profiles-what-a-space-or-collection-shows)
below). The owner can rename their own space, and an instance admin can rename any *shared* space —
a deliberate, narrow exception to "admins get nothing here", because a label is
not a way in. An admin who renames a space still can't list a single item in it,
see its members, or add to it. Nobody renames somebody else's personal shelf,
and a personal space's slug is fixed either way.

Changing a slug changes the space's URL and there's no redirect from the old
one, so links already shared will 404. Nothing stored points at a slug — API
tokens carry scope labels and collection ids — so no access breaks.

Manage members under Settings → Spaces. You pick people from a dropdown of
everyone with an account. Removing someone revokes their access but leaves the
content they created — it belongs to the space, not to them.

![Settings → Spaces with a shared Standards space expanded: its owner, a viewer, and the picker for adding someone](docs/screenshots/sharing.png)

### Profiles: what a space or collection shows

A Standards library is read by Designation and Edition; a paper library by
Creator. A **profile** lets a space or a collection say so: a description shown
in the page header, and the columns its library table shows by default — the
table's own (Title, Creator, Type, Tags, …) or any metadata field.

- **A space's profile** — together with its name and slug — is under Settings →
  Spaces → **Profile**. Editors can change the description and columns; renaming
  stays with the owner.
- **A collection's profile** is on its **⋯** menu or a right-click in the rail →
  **Edit profile…**. Collections a space inherits have the menu too, editable
  by editors of the space they belong to.

Columns are inherited: a collection without its own takes its nearest parent's,
then its space's, then the built-in default. An inherited collection falls back
to the space it lives in, so a Standards folder looks like Standards wherever
it's browsed from. Descriptions aren't inherited.

Everyone can still change their own view from the **Columns** menu; that's
remembered in their browser, per profile, with a reset back to the profile's
columns. Editors also get **Save as default for …** there, which writes their
view to the profile for everyone.

People appear in that dropdown when they first sign in — nothing is synced from
the identity provider ahead of that, so assigning someone the app in Entra (or
Authentik, or anywhere else) doesn't create a shelf account until they actually
log in. To add someone to a space before then, **admins can pre-provision an
account by email** from Settings → Admin. It creates the account and their
personal space with no identity attached; their first sign-in links onto it
by email rather than making a second account. Use the same address the
provider sends, or they'll get that second account.

That dropdown lists every account's display name and email address to **any
signed-in user**. Everyone owns their personal space and so may need to share
it, which is why it isn't admin-only. Nothing else is exposed — no roles, no
identities, nothing about anyone's library.

### Inheriting a space

A space can subscribe to another and read its items without holding a copy.
The shape it's for: one shared **Standards** space holds one copy of each
standard, and every project space — and every person who wants them in their own
library — subscribes to it. Corrections happen once and reach everyone.

Two sides have to agree, and the two panels live under Settings → Spaces →
**Inheritance**:

- The space **being read** opts in: its owner ticks *Let other spaces inherit
  this one*. That's the consent, because everyone who can read a subscribing
  space will be able to read this one's items.
- The space **doing the reading** subscribes: its owner picks from the list of
  spaces that have opted in.

Inherited items are read-only wherever they're borrowed — the API refuses the
writes, not just the UI — and are marked as inherited in the detail panel. Three
properties keep the grant honest:

- **It grants read and nothing else.** An editor on a project space is still a
  viewer on the standards it inherits, and is not a member of that space.
- **It does not chain.** A inherits B, B inherits C — A does not see C. One hop
  is what the model promises, so an owner can answer "who can see my items" from
  one table.
- **The owner keeps control.** *Inherited by* lists every subscriber with a
  Revoke button. Turning the flag back off stops new subscriptions and leaves
  existing ones alone — access silently evaporating across every project is a
  worse surprise than a stale subscription.

Personal spaces can subscribe to a shared space but can't be subscribed *to*.

#### Notes and highlights on someone else's document

Once one PDF is read by the whole company, "everyone who can read this document"
is the wrong audience for a working note. So notes and highlights carry a
visibility:

| | Who sees it |
|---|---|
| `private` | the author, and nobody else — not even the owner of the space the document lives in |
| `space` | everyone who can read the space that owns the **item** |

On an inherited document a new note or highlight starts **private**. Share it and
it reaches the space that owns the document — the other subscribers to Standards,
not your own shelf where nobody is. Only the author can share one or take it
back; an owner can't publish your notes for you, and can't retract them either.

Writing a note needs only read access — annotating a document you can only read
is the whole point. In a space you're actually a member of, notes keep the
behaviour they always had and start shared with that space.

### Engineering standards

Standards get republished, and which edition applies is a decision a project
makes deliberately. Two things the plain item model can't express:

**Revisions know about each other.** Pick the *Engineering Standard* item type,
fill in the issuing body, designation and edition, then file it under a standard
from the item's detail panel. Editions matched on body + designation (ignoring
case) share one history, so the detail panel gets a dropdown of every edition
you can open and a badge saying whether this is the current one.

![An engineering standard's details: its edition, the badge marking it the latest, and the dropdown of both revisions](docs/screenshots/standard-revisions.png)

"Latest" means the newest edition *you can read*, not the newest one on the
instance. When the instance holds something newer that you can't reach, the panel
says so instead of presenting a stale edition as current. An edition with no
issue date sorts last and is never latest — "Rev. 5" and "2020" can't be compared
to each other, so only dates are trusted.

**A space pins the edition it uses.** A project space inheriting Standards can
pin "we build to the 2018 edition"; its library then lists that one and hides the
other four. *Show all revisions* in the library toolbar reveals them, and the
Standards space itself always stays the complete record. Pinning takes owner, not
editor — it changes what everyone else in the space sees by default. Pins survive
dropping and re-adding a subscription.

### Copying an item to another space

**Copy to another space** in the item detail panel makes a real copy: a new item
with its own files. Metadata, files, extracted page text and tags come across;
tags are matched by name into the target space's own tags. Collections don't —
folders are the target's own structure. Neither do notes and highlights: they
belong to whoever wrote them, under the visibility they chose, and republishing
them into a space those people may not be in is a disclosure rather than a copy.

If the other space only needs to *read* the document, inherit instead. One copy
of the bytes, one place to fix a mistake, and everyone sees the fix.

### API tokens

Mint them under Settings → API tokens; the `shelf` CLI and any script you write
authenticate with one. The plaintext is shown exactly once and there is no other
path to it. A token carries coarse scopes — `upload`, `search`, `download` — and
acts as the user who minted it, so by default it reaches every space that user
can: their own, any shared with them, and any those inherit.

Two optional allow-lists narrow it further:

- **Spaces** — the coarse cut, and usually the one you want. A token for an
  import script that should only touch one project, or a read-only token that
  should see the shared Standards space and nothing of your own.
- **Collections** — finer, within a space, optionally including everything
  nested below the ones you pick (resolved at request time, so subcollections
  added later are covered).

Neither can widen access. Both are intersected with what the user can read on
every request, so a token outlives neither a revoked membership nor a dropped
subscription — and a space allow-list is stored as ids, not slugs, so renaming
a space doesn't quietly break it.

![Minting a token: a name, its scopes, and the optional space and collection allow-lists](docs/screenshots/api-tokens.png)

Admin is deliberately unreachable by token: no scope grants it, so a leaked
script token can't reach the admin routes. For what a token can call, see
[The REST API](./DEVELOPERS.md#the-rest-api).

### Admins

Two *instance* roles, `user` and `admin`. Everyone is a `user`; admins
additionally get an Admin tab in Settings, which lists everyone on the instance,
hands out roles, and can add an account from an email address before its owner
has ever signed in (see [Sharing a space](#sharing-a-space)).

Being an admin does **not** grant access to anyone's spaces. Handing out roles
and reading everybody's library are different powers, and keeping them apart
makes the admin role far less dangerous to hold. An admin who needs a space asks
its owner, like anyone else.

## Running your own instance

The application image, `ghcr.io/krande/shelf`, serves the API and the web
interface, and runs the CPU OCR worker too, started with a different command.
It isn't a whole deployment on its own. It needs PostgreSQL and an
S3-compatible object store next to it, plus NATS for background processing. OCR for poor-quality scans uses a separate GPU worker image built
from `Dockerfile.gpu`.

A Helm chart for the application and its worker is in
[`deploy/helm/shelf/`](./deploy/helm/shelf/), and `compose.yaml` runs the
supporting services locally. Configuration, SSO, the OCR workers and local
development are in [DEVELOPERS.md](./DEVELOPERS.md).

## Contributing

Issues and PRs are welcome, though I make no promises about response time. See [DEVELOPERS.md](./DEVELOPERS.md) for setting up, running the tests, and how releases are cut.

## License

[GNU AGPL v3](./LICENSE). If you run a modified shelf as a network service, your modifications have to be available to its users.
