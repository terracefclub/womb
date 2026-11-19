// Build-time only: renders the reviewed TFC production realm with sops-nix
// placeholders in place of the four client secrets. The output holds no secret;
// sops-nix substitutes the values on the host, into memory, at activation.
import { productionRealm } from './identity/tools/production-realm'

const [web, cms, placeholders] = process.argv.slice(2)
const credentials = JSON.parse(placeholders ?? '')
process.stdout.write(JSON.stringify(productionRealm(web, cms, credentials), null, 2) + '\n')
