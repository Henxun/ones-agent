"""Verified ONES web GraphQL operations for recorded work hours."""

from __future__ import annotations


LIST_MANHOURS = """query UserManhoursTimeSeries {
  buckets(groupBy: $manhoursGroupBy) {
    key startDate
    manhours(filter: $manhoursFilter, orderBy: $manhoursOrderBy) {
      key hours owner { uuid } startTime(unit: ONESDATE)
      canBrowseTask canUpdateRecordedHour description type
      task { key uuid name }
    }
  }
}"""

ADD_MANHOUR = """mutation AddManhour {
  addManhour (mode: $mode owner: $owner task: $task type: $type
    start_time: $start_time hours: $hours description: $description) { key }
}"""

UPDATE_MANHOUR = """mutation UpdateManhour {
  updateManhour (mode: $mode owner: $owner task: $task type: $type
    start_time: $start_time hours: $hours description: $description key: $key) { key }
}"""
