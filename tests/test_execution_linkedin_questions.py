from jobbot.execution.linkedin import extract_linkedin_question_widgets


def test_extract_linkedin_question_widgets_routes_low_confidence_to_assist():
    html = """
    <html>
      <body>
        <form>
          <label for="firstName">First name</label>
          <input id="firstName" name="firstName" type="text">
          <input name="customQuestion_42" type="text">
        </form>
      </body>
    </html>
    """

    result = extract_linkedin_question_widgets(page_html=html)

    assert result.question_count == 2
    assert result.assist_required is True
    assert result.recommended_mode == "assist"
    assert result.unknown_field_count == 1
    assert any(row.source == "label_for" for row in result.questions)
    assert any(row.source == "name_attr" for row in result.questions)


def test_extract_linkedin_question_widgets_can_stay_in_draft_mode():
    html = """
    <html>
      <body>
        <form>
          <label for="emailAddress">Email address</label>
          <input id="emailAddress" name="emailAddress" type="email">
          <label for="phoneNumber">Phone number</label>
          <input id="phoneNumber" name="phoneNumber" type="tel">
        </form>
      </body>
    </html>
    """

    result = extract_linkedin_question_widgets(page_html=html)

    assert result.question_count == 2
    assert result.assist_required is False
    assert result.recommended_mode == "draft"
    assert result.unknown_field_count == 0
