import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const lock = JSON.parse(
  readFileSync(new URL('../../package-lock.json', import.meta.url), 'utf8'),
)

const requiredLightningCssPackages = [
  'lightningcss-android-arm64',
  'lightningcss-darwin-arm64',
  'lightningcss-darwin-x64',
  'lightningcss-freebsd-x64',
  'lightningcss-linux-arm-gnueabihf',
  'lightningcss-linux-arm64-gnu',
  'lightningcss-linux-arm64-musl',
  'lightningcss-linux-x64-gnu',
  'lightningcss-linux-x64-musl',
  'lightningcss-win32-arm64-msvc',
  'lightningcss-win32-x64-msvc',
]

describe('frontend dependency lock portability', () => {
  it.each(requiredLightningCssPackages)(
    'locks the %s optional native package at the Lightning CSS version',
    (packageName) => {
      const lightningCss = lock.packages['node_modules/lightningcss']
      const nativePackage = lock.packages[`node_modules/${packageName}`]

      expect(nativePackage).toBeDefined()
      expect(nativePackage?.dev).toBe(true)
      expect(nativePackage?.optional).toBe(true)
      expect(nativePackage?.version).toBe(lightningCss.version)
    },
  )
})
