import { useTranslation } from 'react-i18next'
import { Globe } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  supportedLanguages,
  languageNames,
  languageFlags,
  changeLanguage,
  type SupportedLanguage,
} from '@/i18n'

export function LanguageSwitcher() {
  const { i18n } = useTranslation()

  const handleLanguageChange = (lang: SupportedLanguage) => {
    // Loads the target language's bundle (lazy chunk) before switching.
    void changeLanguage(lang)
  }

  const currentLanguage = (i18n.language || 'en') as SupportedLanguage

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="h-9 w-9">
          <Globe className="h-4 w-4" />
          <span className="sr-only">Change language</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {supportedLanguages.map((lang) => (
          <DropdownMenuItem
            key={lang}
            onClick={() => handleLanguageChange(lang)}
            className={currentLanguage === lang ? 'bg-accent' : ''}
          >
            <span className="mr-2">{languageFlags[lang]}</span>
            {languageNames[lang]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
