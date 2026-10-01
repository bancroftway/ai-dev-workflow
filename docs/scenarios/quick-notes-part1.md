# Quick Notes — initial application

Build a small single-page web app called **Quick Notes** for jotting down short notes. One user, no sign-in.

## Features

1. **Add a note.** A text box and an "Add" button at the top of the page. A note is plain text, 1 to 200 characters. Pressing Add with an empty or whitespace-only box shows the inline message "Note can't be empty" and adds nothing. Text longer than 200 characters shows "Note is too long (max 200)" and adds nothing. After a successful add, the text box clears.
2. **List notes.** Below the text box, show every note, newest first. Each row shows the note text and the time it was created (for example "2:05 PM"). When there are no notes, show "No notes yet".
3. **Delete a note.** Each row has a "Delete" button that removes that note immediately, with no confirmation.
4. **Pin a note.** Each row has a pin toggle (a pin icon button). Pinned notes always appear at the top of the list, above unpinned notes, and show a "Pinned" badge. Clicking the toggle again unpins the note.
5. **Note count.** The page header shows "N notes", for example "3 notes" or "1 note".

## Data

Notes are stored by the backend API, so they survive a page reload. Each note has an id, text, created time and a pinned flag. Data only needs to persist while the server is running; a real database is not required.

## Out of scope

Editing note text, sign-in, multiple users, categories, search.
