# Hosting boundary only. App client/role mappings belong to TFC's tested identity contract.
def https_origin: type == "string" and test("^https://[A-Za-z0-9.-]+(:[0-9]+)?$");
def https_redirect: type == "string" and test("^https://[A-Za-z0-9.-]+(:[0-9]+)?/") and (contains("*") | not);
type == "object" and .realm == "terrace" and .enabled == true and
.registrationAllowed == false and (.sslRequired == "external" or .sslRequired == "all") and
((.users // []) | length == 0) and ((.federatedUsers // []) | length == 0) and
((.identityProviders // []) | length == 0) and ((.identityProviderMappers // []) | length == 0) and
((.userFederationProviders // []) | length == 0) and ((.userFederationMappers // []) | length == 0) and
((.components["org.keycloak.storage.UserStorageProvider"] // []) | length == 0) and
all(.clients[]?;
  .directAccessGrantsEnabled == false and .implicitFlowEnabled != true and .serviceAccountsEnabled != true and
  all(.redirectUris[]?; https_redirect) and all(.webOrigins[]?; https_origin))
