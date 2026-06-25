import { memo, useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

interface StreamingTextProps {
  text: string
  /** 为 true 时在末尾显示闪烁光标（模型仍在流式输出）。 */
  active?: boolean
  className?: string
}

/** 相邻字符 reveal 的基础间隔。越小越快越平滑；60ms 是偏慢的打字机节奏。 */
const BASE_DELAY_MS = 60

/** 落后目标文本多少个字符时进入追速模式，避免模型一次吐大段导致滞后太久。 */
const CATCH_UP_THRESHOLD = 14

/** 追速模式下每个 tick Reveal 多少个字符。 */
const CATCH_UP_BATCH = 3

/**
 * 渲染模型实时流式文本，带平滑打字机效果和闪烁光标。
 * 用 memo 包裹，因为底层字符串每个 token delta 都会更新，避免无关重渲染抖动。
 */
export const StreamingText = memo(function StreamingText({
  text,
  active = false,
  className,
}: StreamingTextProps) {
  const [displayed, setDisplayed] = useState(text)
  const targetRef = useRef(text)

  // 让 targetRef 与 prop 同步，但不在每次渲染时触发额外的重渲染循环。
  useEffect(() => {
    targetRef.current = text
  }, [text])

  // 平滑打字机循环。
  useEffect(() => {
    if (!active) {
      // 流已结束：下一帧直接显示完整文本。
      if (displayed !== text) {
        const id = requestAnimationFrame(() => setDisplayed(text))
        return () => cancelAnimationFrame(id)
      }
      return
    }

    // 目标文本变短（如重置）时，下一帧同步回退。
    if (text.length < displayed.length) {
      const id = requestAnimationFrame(() => setDisplayed(text))
      return () => cancelAnimationFrame(id)
    }

    if (displayed.length >= targetRef.current.length) {
      // 已经追上；等 text 变化后 effect 会自动重新启动。
      return
    }

    const behind = targetRef.current.length - displayed.length
    const delay = behind > CATCH_UP_THRESHOLD ? BASE_DELAY_MS / 2 : BASE_DELAY_MS
    const charsToAdd = behind > CATCH_UP_THRESHOLD ? CATCH_UP_BATCH : 1

    const timer = setTimeout(() => {
      const nextLength = Math.min(displayed.length + charsToAdd, targetRef.current.length)
      setDisplayed(targetRef.current.slice(0, nextLength))
    }, delay)

    return () => clearTimeout(timer)
  }, [text, displayed, active])

  return (
    <pre
      className={cn(
        "whitespace-pre-wrap break-words font-mono text-sm leading-relaxed text-foreground",
        className
      )}
    >
      {displayed}
      {active && (
        <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-[3px] animate-pulse bg-primary" />
      )}
    </pre>
  )
})

export default StreamingText
