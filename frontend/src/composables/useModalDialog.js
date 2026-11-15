import { watch, nextTick, onBeforeUnmount } from 'vue'

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

// Keyboard behaviour for a modal that is shown with v-if and marked role="dialog"
// aria-modal="true": Escape closes it, focus moves into it when it opens, Tab stays inside it,
// and focus goes back to whatever opened it when it closes.
//
// The dialog is found in the document instead of through a template ref, so a view only has to
// call this next to its "is open" ref. With several open, the last one in the document is on
// top, and that is the one the keys act on.
export function useModalDialog(isOpen, close) {
  let opener = null
  let dialog = null

  // Hidden controls (display: none, collapsed sections) cannot take focus; skip them.
  const focusable = () => (dialog ? [...dialog.querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length) : [])

  const onKeydown = (e) => {
    if (e.key === 'Escape') {
      close()
      return
    }
    if (e.key !== 'Tab' || !dialog) return
    const items = focusable()
    if (!items.length) {
      e.preventDefault()
      return
    }
    const first = items[0]
    const last = items[items.length - 1]
    const inside = dialog.contains(document.activeElement)
    // Wrap at either end, and pull focus back in if it got out (a click on the page behind).
    if (e.shiftKey && (!inside || document.activeElement === first)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
      e.preventDefault()
      first.focus()
    }
  }

  // Listens on window, not on the overlay: a keydown on the overlay only fires while focus is
  // already inside it. The listener exists only while the modal is open.
  watch(isOpen, async (open) => {
    if (open) {
      opener = document.activeElement
      window.addEventListener('keydown', onKeydown)
      await nextTick()
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]')
      dialog = dialogs[dialogs.length - 1] || null
      // The first field, not the close button: opening a form should let you start typing.
      const items = focusable()
      const target = items.find((el) => el.matches('input, select, textarea')) || items[0]
      target?.focus()
    } else {
      window.removeEventListener('keydown', onKeydown)
      dialog = null
      // Only if the opener is still on the page (a deleted row's button is not).
      if (opener && document.contains(opener)) opener.focus()
      opener = null
    }
  }, { immediate: true })

  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
}
