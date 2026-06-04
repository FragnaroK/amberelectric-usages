"""Amber Electric - Usages Coordinator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from amberelectric import ApiException
from amberelectric.api import amber_api
from amberelectric.models.usage import Usage
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import CURRENCY_DOLLAR, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER


class AmberUsagesCoordinator(DataUpdateCoordinator):
    """Handle and update past grid usage data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: amber_api.AmberApi,
        site_id: str,
        entry_title: str,
    ) -> None:
        """Initialise the data service."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
        )
        self.lastest_time: datetime = None
        self.site_id = site_id
        self._hass = hass
        self._api = api
        
        # Cleaned up naming conventions for valid HA statistic identifiers
        clean_title = entry_title.lower().replace("-", "").replace(" ", "_")
        self._usage_statistic_id_prefix = f"{DOMAIN}:{clean_title}_usages"
        self._cost_statistic_id_prefix = f"{DOMAIN}:{clean_title}_usage_costs"

    def _get_usages(self) -> list[Usage]:
        """Fetch historical usage data in clean, non-overlapping 7-day windows."""
        usages: list[Usage] = []
        today = dt_util.now().date()
        
        # Amber API allows maximum 7 days per call. 
        # Shift back exactly 7 days per iteration to avoid overlapping data on boundary days.
        for offset in range(4):
            end_date = today - timedelta(days=offset * 7)
            start_date = end_date - timedelta(days=7)
            
            try:
                usages += self._api.get_usage(
                    self.site_id, start_date=start_date, end_date=end_date
                )
            except ApiException as api_exception:
                LOGGER.error("Failed to batch fetch Amber data for range %s to %s: %s", start_date, end_date, api_exception)
                raise api_exception
                
        return usages

    async def _async_update_data(self) -> None:
        """Fetch and structure incoming usage profiles."""
        raw_usages: list[Usage] = []
        try:
            raw_usages = await self._hass.async_add_executor_job(self._get_usages)
        except ApiException as api_exception:
            raise UpdateFailed("Missing usage data, skipping update") from api_exception

        LOGGER.debug("Fetched new Amber data: %s", raw_usages)

        usages_by_hour_by_channel: dict[str, dict[datetime, list[Usage]]] = {}
        for usage in raw_usages:
            usages_by_hour_by_channel.setdefault(usage.channel_identifier, {})

            # Standardize and force all bucket keys to exact UTC hours to safely match database records
            start_time_hour = (
                usage.start_time - timedelta(
                    minutes=usage.start_time.minute,
                    seconds=usage.start_time.second,
                    microseconds=usage.start_time.microsecond,
                )
            ).astimezone(timezone.utc)

            usages_by_hour_by_channel[usage.channel_identifier].setdefault(start_time_hour, [])
            usages_by_hour_by_channel[usage.channel_identifier][start_time_hour].append(usage)

        await self._insert_usage_statistic(usages_by_hour_by_channel)
        await self._insert_cost_statistic(usages_by_hour_by_channel)

    async def _insert_usage_statistic(
        self, usages_by_hour_by_channel: dict[str, dict[datetime, list[Usage]]]
    ) -> None:
        """Process usage and pass off to external statistics."""
        for channel, usages_by_hour in usages_by_hour_by_channel.items():
            statistic_id = f"{self._usage_statistic_id_prefix}_{channel.lower()}"
            LOGGER.debug("Updating %s", statistic_id)
            
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{self._usage_statistic_id_prefix} - {channel}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )

            last_stat_sum: float = 0
            last_stat_start: datetime = None
            
            # get_last_statistics is already an async-safe DB query. Do not wrap in executor job.
            last_stats = await get_last_statistics(self._hass, 1, statistic_id, True, {"sum"})
            
            if last_stats and statistic_id in last_stats:
                last_stat = last_stats[statistic_id][0]
                last_stat_sum = last_stat["sum"] or 0
                last_stat_start = datetime.fromtimestamp(last_stat["start"], timezone.utc)

            statistics: list[StatisticData] = []
            
            # CRITICAL: Sorted chronologically so cumulative updates compound accurately
            for start_hour, usages in sorted(usages_by_hour.items()):
                if last_stat_start is not None and start_hour <= last_stat_start:
                    continue

                total_kwh: float = sum(usage.kwh for usage in usages)
                last_stat_sum += total_kwh

                statistics.append(
                    StatisticData(state=total_kwh, sum=last_stat_sum, start=start_hour)
                )

                if self.lastest_time is None or self.lastest_time < start_hour:
                    self.lastest_time = start_hour

            if statistics:
                async_add_external_statistics(self._hass, metadata, statistics)

    async def _insert_cost_statistic(
        self, usages_by_hour_by_channel: dict[str, dict[datetime, list[Usage]]]
    ) -> None:
        """Process pricing details and append to external financial statistics."""
        for channel, usages_by_hour in usages_by_hour_by_channel.items():
            statistic_id = f"{self._cost_statistic_id_prefix}_{channel.lower()}"
            LOGGER.debug("Updating %s", statistic_id)
            
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{self._cost_statistic_id_prefix} - {channel}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=CURRENCY_DOLLAR,
            )

            last_stat_sum: float = 0
            last_stat_start: datetime = None
            
            # Natively async API called directly
            last_stats = await get_last_statistics(self._hass, 1, statistic_id, True, {"sum"})
            
            if last_stats and statistic_id in last_stats:
                last_stat = last_stats[statistic_id][0]
                last_stat_sum = last_stat["sum"] or 0
                last_stat_start = datetime.fromtimestamp(last_stat["start"], timezone.utc)

            # Keep initial DB tracking structure clear prior to inversion checks
            if channel == "B1":
                last_stat_sum = -last_stat_sum

            statistics: list[StatisticData] = []
            
            # CRITICAL: Sorted chronologically so cumulative updates compound accurately
            for start_hour, usages in sorted(usages_by_hour.items()):
                if last_stat_start is not None and start_hour <= last_stat_start:
                    continue

                total_cost: float = sum(usage.cost / 100 for usage in usages)
                last_stat_sum += total_cost

                if channel == "B1":
                    statistics.append(
                        StatisticData(state=-total_cost, sum=-last_stat_sum, start=start_hour)
                    )
                else:
                    statistics.append(
                        StatisticData(state=total_cost, sum=last_stat_sum, start=start_hour)
                    )

                if self.lastest_time is None or self.lastest_time < start_hour:
                    self.lastest_time = start_hour

            if statistics:
                async_add_external_statistics(self._hass, metadata, statistics)