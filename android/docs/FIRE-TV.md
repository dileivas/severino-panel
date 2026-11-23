# Fire TV: D-pad fixes for the request dialog

Two fixes for Fire TV remotes in `spatnav.js`, found on a Fire TV Stick 4K (Fire OS 6.7.1.1, Android 7.1, SDK 25)
driving a Seerr 2.x instance. Both are in the shared script, so Google TV / Android TV benefit too.

## 1. The request dialog trapped the focus behind it

`View Request` on a title opens a dialog with **Close** and **Cancel Request**. Arrow keys did nothing: the
highlight stayed on the `View Request` button *behind* the dialog.

Cause: `pick()` has a shortcut for detail pages — it focuses the primary action inside `.media-actions`, preferring a
label matching `/request|play on|…/i`. With the dialog open, `candidates()` correctly returned the dialog's two buttons,
but `pick()` then jumped to `.media-actions` (still in the DOM, behind the overlay) and re-focused `View Request` on
every move.

Fix: the shortcut is skipped while an overlay is open.

```js
var ma = visibleOverlay() ? null : document.querySelector('.media-actions');
```

## 2. BACK did not close the dialog

`doBack()` already sent `Escape` to the document and to the focused element. This dialog ignores Escape entirely (it
only closes on a click), so BACK fell through and left the dialog on screen.

Fix: `Escape` is now also dispatched on `window`, and if the overlay is still there ~90 ms later, `closeButton()`
clicks its dismiss affordance — a button named *Close* / *Dismiss* / *Back* (any language, matched on
`aria-label` + text), falling back to a bare *Cancel*. Destructive buttons are never matched. Focus then returns to
whatever opened the dialog.

## Verified

On the device, with a real remote: `View Request` → ↓ moves the highlight to **Close**; BACK closes the dialog, stays
on the title page and returns the highlight to **View Request**.

## Note

The APK built with these changes is signed with its own key, so it cannot be installed over a build signed by
someone else — uninstall first (that also clears the saved server address and the session).
