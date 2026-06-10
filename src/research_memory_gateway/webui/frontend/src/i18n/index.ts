import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhCN from './zh-CN.json'
import en from './en.json'

const savedLanguage = localStorage.getItem('i18nextLng') || 'zh-CN'

i18n
  .use(initReactI18next)
  .init({
    resources: {
      'zh-CN': { translation: zhCN },
      en: { translation: en }
    },
    lng: savedLanguage,
    fallbackLng: 'zh-CN',
    interpolation: {
      escapeValue: false
    }
  })

export default i18n
