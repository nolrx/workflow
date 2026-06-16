/**
 * API exports
 */
export { apiClient, api, tokenManager } from "./client"
export type { ApiResponse, ApiError } from "./client"

// PPT API
export * as pptApi from "./ppt"
export type {
  Material,
  ReferenceFile,
  UserTemplate,
  PPTSettings,
  ImageVersion,
  OutputLanguage,
} from "./ppt"
