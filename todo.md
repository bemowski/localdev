
* Created DematicNonProd/localdev
  - added Google Auth Platform / OAuth client - localdev-sso, redirect url localhost/oauth2/callback  (maybe should add keycloak)
  - enabled secrets manager api
  - added oauth client secret to secrets manager.
  - TODO: iam service account that can read secrets
     - service account key (static) for secrets manager.


* valid certs
  - dns name
  - certmanager

* Dematic
  - usage server in stage 
    - add argo app-of-apps deploy
    - add mise based build/version/push to gcloud registry
    - 

  - jenkins --> localdev
  - sortation --> localdev

  - create a helm template - as opposed to "horizon operator"

* Formalize the "component" 
  - are there existing constructs
  - simple -> complex

  - build a docker image, deploy
  - docker image needs secrets (how to handle secrets stores)
  - docker image needs a postgres database  (Job for initialization)
    - shared database in postgres namespace
    - cnpg database creation, then use
    - abstract init to Job
  - docker image connects to nats jetstream
  - docker image publishes an API outside the cluster

