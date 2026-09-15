CREATE DATABASE IF NOT EXISTS `fleetbase_sandbox`
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON `fleetbase_sandbox`.* TO 'fleetbase'@'%';

CREATE DATABASE IF NOT EXISTS `fleetbase_storefront`
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON `fleetbase_storefront`.* TO 'fleetbase'@'%';
