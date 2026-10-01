# Quick Notes — changes

Three changes to the existing Quick Notes app. Everything not mentioned here stays as it is.

## Change: shorter notes

The maximum note length drops from 200 to **120 characters**. Text longer than 120 characters shows "Note is too long (max 120)" and adds nothing. Show a live character counter under the text box, for example "37 / 120".

## Remove: pinning

Remove the **Pin a note** feature completely: the pin toggle, the "Pinned" badge and the rule that pinned notes sort first. The list goes back to plain newest-first order. Remove the pinned flag from the API and data model, and delete the tests that covered pinning.

## New: search notes

Add a search box above the list. Typing filters the list to notes whose text contains the search text, ignoring upper/lower case, as you type. When nothing matches, show "No matching notes". Clearing the box shows every note again. The header count ("N notes") always shows the total number of notes, not the filtered number.
