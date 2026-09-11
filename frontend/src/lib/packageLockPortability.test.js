import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const lock = JSON.parse(
  readFileSync(new URL('../../package-lock.json', import.meta.url), 'utf8'),
)
const manifest = JSON.parse(
  readFileSync(new URL('../../package.json', import.meta.url), 'utf8'),
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

// The property: `npm ci` on ANY platform CI might run on must resolve a
// Lightning CSS native binary at the same version as lightningcss itself.
// Without the full set pinned, a lock generated on a Mac installs no binary on
// the Linux runner and the build dies at "Cannot find module
// ../lightningcss.<platform>.node".
//
// This used to also assert `dev: true` on each entry. That premise EXPIRED:
// the packages are declared in `optionalDependencies`, which is a PRODUCTION
// section, so marking them dev-only was always wrong and npm 10.9.8 no longer
// writes the flag — a plain `npm install` of the unmodified baseline drops it.
// Verified by reinstalling HEAD's own package.json/package-lock.json in a clean
// directory: the flag disappears with no source change at all.
//
// The replacement is strictly STRONGER, not a relaxation. `dev` said nothing
// about portability; these three clauses say what actually has to hold:
//
//   1. the manifest DECLARES the package, so the lock cannot drift from intent;
//   2. the lock marks it `optional`, so a platform that cannot use it does not
//      fail the install;
//   3. its version equals lightningcss's, which is the real ABI constraint.
describe('frontend dependency lock portability', () => {
  it('declares every platform native in the manifest', () => {
    // Asserting against the manifest as well as the lock is what stops a
    // regenerated lock from quietly shrinking the set.
    expect(Object.keys(manifest.optionalDependencies ?? {}).sort())
      .toEqual([...requiredLightningCssPackages].sort())
  })

  it.each(requiredLightningCssPackages)(
    'locks the %s optional native package at the Lightning CSS version',
    (packageName) => {
      const lightningCss = lock.packages['node_modules/lightningcss']
      const nativePackage = lock.packages[`node_modules/${packageName}`]

      expect(nativePackage).toBeDefined()
      expect(nativePackage?.optional).toBe(true)
      expect(nativePackage?.version).toBe(lightningCss.version)
      expect(lock.packages['']?.optionalDependencies?.[packageName]).toBeDefined()
    },
  )
})
