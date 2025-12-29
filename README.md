# catlink Lazycat Native Cloud App

This project is adapted based on Xpra, with fixed several interaction bugs and added essential interfaces to support the functionalities of the Lazycat Cloud CDE (Lazycat Native Cloud Desktop Environment).

Catlink is modified from Xpra and serves as the rendering layer for the CDE environment. All parts involving Xpra logic modifications are committed to this repository under the GPL2 license.

CDE itself is not open-source at present. Besides Catlink, it also involves the following components:
- CJK Input method integration
- Clipboard integration (image, uri-list)
- Integration of advanced selection interactions such as DND
- Encoding optimization
- Deep integration with Lazycat Cloud (file system & permissions)
- GUI Application manage
