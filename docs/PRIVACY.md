# Monica Desktop privacy

Effective date: November 2, 2026

Monica Desktop keeps its relationship database, imported records, downloaded
media, change history, source credentials and settings on your Mac. There is no
Monica-hosted account, analytics service or advertising service in this desktop
application. Your local account authenticates access to the local web interface.

## Sources you connect

Importing is optional. When you connect Instagram or LinkedIn, the application
communicates directly with that service using the account or session you supply.
Those services receive the requests, account identifiers, session information and
network information needed to provide their service, under their own policies.
Imported names, profiles, posts, messages and photos are stored locally.

The WeChat connection reads the local directory you choose with keys you supply.
Local WeChat records are processed on your Mac. Only connect accounts and records
that you are entitled to access. Media import may contact a source's media servers
to retrieve content you ask to import.

## Optional AI

AI is off on a new installation. If you configure and enable an AI service,
Monica sends the content needed for the enabled task directly to the endpoint you
configure. Depending on the task, this can include names and contact information,
profile identifiers, messages, notes, relationship information or images. Your
chosen provider receives those requests and may retain them according to its
policy and your account settings. A local compatible endpoint can be used.
Review the endpoint and provider policy before enabling AI for private records.

## Optional face processing

Face processing runs locally. The public application does not include InsightFace
model weights. If you provide compatible weights and enable the feature, face
embeddings and grouping information are stored in your local database. The
application does not send face embeddings to a Monica service.

## Storage and deletion

The Mac App Store build stores its working files in its macOS application
container. Direct-download builds use the Monica folders under your user Library.
Source sessions and keys are stored in private local files. Protect your macOS
account and backups. Disconnecting a source stops future collection; it does not
erase records that you already imported. You control local records, exports and
backups. Erase these separately when you want to remove them. Removing Monica does
not remove copies held by a source, an AI provider, or your backup service.

## Support

Use the [project issue tracker](https://github.com/hmumixaM/monica-desktop/issues)
for questions about the application or this policy. Issues are public. Do not
post credentials, keys, private contact records, messages or unredacted logs.

The project does not sell personal data or use it for cross-app advertising.
