import { useTranslation } from "react-i18next"

interface PreviewCaptionProps {
  alt?: string
}

export function PreviewCaption({ alt }: PreviewCaptionProps) {
  const { t } = useTranslation("common")

  return (
    <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent px-4 py-3 text-center text-xs text-white/70 sm:py-4">
      {alt && <p className="mb-1 font-medium">{alt}</p>}
      <p>{t("preview.hint")}</p>
    </div>
  )
}
