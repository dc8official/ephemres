import { watch, onBeforeUnmount } from 'vue'

// Closes a modal on Escape. Listens on window, not on the overlay, because these
// modals do not move focus inside themselves, so a keydown on the overlay would
// never fire. The listener exists only while the modal is open.
export function useEscapeClose(isOpen, close) {
  const onKeydown = (e) => {
    if (e.key === 'Escape') close()
  }
  watch(isOpen, (open) => {
    if (open) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  }, { immediate: true })
  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
}
