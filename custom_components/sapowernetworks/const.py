"""Constants for SA Power Networks."""

from datetime import UTC, datetime, timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "sapowernetworks"
ATTRIBUTION = "Data provided by SA Power Networks"

DEFAULT_SCAN_INTERVAL = timedelta(hours=24)
INITIAL_BACKFILL_START = datetime(2000, 1, 1, tzinfo=UTC)
DETAILED_REPORT_MAX_RANGE = timedelta(days=90)

PORTAL_BASE_URL = "https://customer.portal.sapowernetworks.com.au"
CAD_SITE_LOGIN_PATH = "/meterdata/CADSiteLogin"
CAD_DASHBOARD_PATH = "apex/cadenergydashboard"
CAD_REQUEST_METER_DATA_PATH = "CADRequestMeterData"

PORTAL_COMPANY = "SAPN"
REPORT_TYPE_DETAILED_CSV = "Detailed Report (CSV)"
REPORT_TYPE_SUMMARY_CSV = "Summary Format (CSV)"
REPORT_DATA_SET_NEM12 = "Customer Access NEM12"

STATISTIC_NAME_PREFIX = "SA Power Networks"
