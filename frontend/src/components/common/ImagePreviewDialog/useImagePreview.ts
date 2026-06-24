import { useCallback, useEffect, useRef, useState } from "react"

const MAX_SCALE = 4
const MIN_SCALE = 1
const ZOOM_STEP = 1.5

interface UseImagePreviewOptions {
  imageCount: number
  currentIndex: number
  onIndexChange?: (index: number) => void
  onClose: () => void
}

interface UseImagePreviewReturn {
  scale: number
  position: { x: number; y: number }
  isDragging: boolean
  canZoomIn: boolean
  canZoomOut: boolean
  zoomIn: () => void
  zoomOut: () => void
  toggleZoom: () => void
  resetZoom: () => void
  goPrev: () => void
  goNext: () => void
  handleMouseDown: (e: React.MouseEvent<HTMLImageElement>) => void
  handleWheel: (e: React.WheelEvent<HTMLImageElement>) => void
}

/**
 * Manages zoom, pan, and gallery navigation for the image preview dialog.
 */
export function useImagePreview({
  imageCount,
  currentIndex,
  onIndexChange,
  onClose,
}: UseImagePreviewOptions): UseImagePreviewReturn {
  const [scale, setScale] = useState(MIN_SCALE)
  const [position, setPosition] = useState({ x: 0, y: 0 })
  const [isDragging, setIsDragging] = useState(false)

  const dragStartRef = useRef({ x: 0, y: 0 })
  const positionStartRef = useRef({ x: 0, y: 0 })

  const resetZoom = useCallback(() => {
    setScale(MIN_SCALE)
    setPosition({ x: 0, y: 0 })
  }, [])

  const canZoomIn = scale < MAX_SCALE
  const canZoomOut = scale > MIN_SCALE

  const clampIndex = useCallback(
    (index: number) => Math.max(0, Math.min(index, imageCount - 1)),
    [imageCount]
  )

  const goPrev = useCallback(() => {
    resetZoom()
    onIndexChange?.(clampIndex(currentIndex - 1))
  }, [clampIndex, currentIndex, onIndexChange, resetZoom])

  const goNext = useCallback(() => {
    resetZoom()
    onIndexChange?.(clampIndex(currentIndex + 1))
  }, [clampIndex, currentIndex, onIndexChange, resetZoom])

  const zoomIn = useCallback(() => {
    setScale((s) => Math.min(MAX_SCALE, s * ZOOM_STEP))
  }, [])

  const zoomOut = useCallback(() => {
    setScale((s) => {
      const next = Math.max(MIN_SCALE, s / ZOOM_STEP)
      if (next === MIN_SCALE) setPosition({ x: 0, y: 0 })
      return next
    })
  }, [])

  const toggleZoom = useCallback(() => {
    if (scale > MIN_SCALE) {
      resetZoom()
    } else {
      setScale(ZOOM_STEP)
    }
  }, [scale, resetZoom])

  const handleMouseDown = useCallback(
    (e: React.MouseEvent<HTMLImageElement>) => {
      if (scale <= MIN_SCALE) return
      e.preventDefault()
      setIsDragging(true)
      dragStartRef.current = { x: e.clientX, y: e.clientY }
      positionStartRef.current = { ...position }
    },
    [scale, position]
  )

  const handleMouseMove = useCallback(
    (e: MouseEvent) => {
      if (!isDragging) return
      const dx = e.clientX - dragStartRef.current.x
      const dy = e.clientY - dragStartRef.current.y
      setPosition({
        x: positionStartRef.current.x + dx,
        y: positionStartRef.current.y + dy,
      })
    },
    [isDragging]
  )

  const handleMouseUp = useCallback(() => {
    setIsDragging(false)
  }, [])

  useEffect(() => {
    window.addEventListener("mousemove", handleMouseMove)
    window.addEventListener("mouseup", handleMouseUp)
    return () => {
      window.removeEventListener("mousemove", handleMouseMove)
      window.removeEventListener("mouseup", handleMouseUp)
    }
  }, [handleMouseMove, handleMouseUp])

  const handleWheel = useCallback(
    (e: React.WheelEvent<HTMLImageElement>) => {
      e.preventDefault()
      if (e.deltaY < 0) zoomIn()
      else zoomOut()
    },
    [zoomIn, zoomOut]
  )

  // Keyboard navigation: arrow keys switch images, Esc is handled by Radix Dialog.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft") goPrev()
      else if (e.key === "ArrowRight") goNext()
      else if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [goPrev, goNext, onClose])

  return {
    scale,
    position,
    isDragging,
    canZoomIn,
    canZoomOut,
    zoomIn,
    zoomOut,
    toggleZoom,
    resetZoom,
    goPrev,
    goNext,
    handleMouseDown,
    handleWheel,
  }
}

export { MAX_SCALE, MIN_SCALE }
